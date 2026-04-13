"""
Figma 온라인상세 내보내기 - Streamlit 웹앱 v1.1
- 어린이과학동아 / 어린이수학동아 / 과학동아 지원
- 완료 후 ZIP으로 다운로드
"""

import re
import io
import zipfile
import datetime
import requests
import streamlit as st
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

COUPANG_MAX_H  = 3000
COUPANG_MAX_MB = 1.0
MAX_WORKERS    = 6
PX_TO_PLATFORM = {"940":"예스24","900":"교보","880":"DS","860":"네이버","700":"알라딘"}
THUMB_MAP = {"ds스토어1000","wh1000","wh900","wh600","wh500","wh458","알라딘_w900","예스24_h600","교보_w458"}
THUMB_SEC     = "컴포넌트-썸네일"
THUMB_KEYWORD = "내보내기"

def api_get(path, token):
    r = requests.get("https://api.figma.com/v1" + path, headers={"X-Figma-Token": token})
    r.raise_for_status()
    return r.json()

def get_image_urls(file_key, node_ids, fmt, token):
    ids = ",".join(node_ids.keys())
    r = requests.get(
        "https://api.figma.com/v1/images/" + file_key,
        headers={"X-Figma-Token": token},
        params={"ids": ids, "format": fmt, "scale": 1.0}
    )
    r.raise_for_status()
    data = r.json()
    if data.get("err"):
        raise Exception("Figma API error: " + str(data["err"]))
    return data.get("images", {})

def get_url(image_urls, node_id):
    return image_urls.get(node_id) or image_urls.get(node_id.replace(":", "-"))

def discover_nodes(file_key, node_id, token):
    data     = api_get("/files/" + file_key + "/nodes?ids=" + node_id + "&depth=2", token)
    canvas   = data["nodes"][node_id]["document"]
    children = canvas.get("children", [])
    sections = {c["name"]: c["id"] for c in children}

    thumb_sec_id  = sections.get(THUMB_SEC)
    detail_sec_id = None
    for c in children:
        if c.get("type") == "SECTION" and c["name"] != THUMB_SEC:
            detail_sec_id = c["id"]
            break

    if not thumb_sec_id or not detail_sec_id:
        raise Exception("섹션을 찾을 수 없습니다. 발견: " + str(list(sections.keys())))

    td = api_get("/files/" + file_key + "/nodes?ids=" + thumb_sec_id + "," + detail_sec_id + "&depth=2", token)

    thumb_nodes = {}
    for c in td["nodes"][thumb_sec_id]["document"].get("children", []):
        if THUMB_KEYWORD in c["name"]:
            cid = c["id"]
            sd  = api_get("/files/" + file_key + "/nodes?ids=" + cid + "&depth=1", token)
            for sub in sd["nodes"][cid]["document"].get("children", []):
                if sub["name"] in THUMB_MAP:
                    thumb_nodes[sub["id"]] = sub["name"]

    detail_nodes   = {}
    coupang_parent = None
    coupang_prefix = ""
    for c in td["nodes"][detail_sec_id]["document"].get("children", []):
        name = c["name"]
        if "780" in name and c["type"] in ("INSTANCE", "FRAME", "COMPONENT"):
            coupang_parent = c["id"]
            m = re.match(r"^(.+?)\(780\)", name)
            if m:
                coupang_prefix = m.group(1)
            continue
        m = re.match(r"^(.+?)\((\d+)\)", name)
        if m:
            platform = PX_TO_PLATFORM.get(m.group(2))
            if platform and c["type"] in ("INSTANCE", "FRAME", "COMPONENT"):
                detail_nodes[c["id"]] = m.group(1) + "(" + m.group(2) + ")_" + platform

    if not thumb_nodes:    raise Exception("썸네일 노드를 찾을 수 없습니다.")
    if not detail_nodes:   raise Exception("상세페이지 노드를 찾을 수 없습니다.")
    if not coupang_parent: raise Exception("쿠팡 부모 노드(780)를 찾을 수 없습니다.")
    return thumb_nodes, detail_nodes, coupang_parent, coupang_prefix

def compress_jpg(img, max_size_mb=None):
    quality = 95
    while quality >= 10:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if max_size_mb is None or buf.tell() / (1024*1024) <= max_size_mb:
            return buf.getvalue()
        quality -= 5
    return buf.getvalue()

def download_nodes_parallel(file_key, node_dict, fmt, token):
    """UI 콜백 없이 순수 병렬 다운로드 - Streamlit 스레드 이슈 방지"""
    image_urls = get_image_urls(file_key, node_dict, fmt, token)
    results    = {}

    def fetch_one(args):
        nid, fname = args
        url = get_url(image_urls, nid)
        if not url:
            return fname, None
        r = requests.get(url)
        r.raise_for_status()
        if fmt == "jpg":
            data = compress_jpg(Image.open(io.BytesIO(r.content)).convert("RGB"), COUPANG_MAX_MB)
        else:
            data = r.content
        return fname, data

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_one, (nid, fname)): fname
                   for nid, fname in node_dict.items()}
        for f in as_completed(futures):
            fname, data = f.result()
            if data:
                results[fname] = data
    return results

def split_recursive(s, max_h, warnings):
    if s["h"] <= max_h:
        return [s]
    if not s.get("split"):
        warnings.append("[강제분할] '" + s["name"] + "' " + str(int(s["h"])) + "px > " + str(max_h) + "px")
    half   = s["h"] / 2.0
    origin = s.get("split_origin", s["name"])
    top_p  = dict(s); top_p["h"] = half; top_p["split"] = "top"; top_p["split_origin"] = origin
    bot_p  = dict(s); bot_p["y_rel"] = s["y_rel"] + half; bot_p["h"] = s["h"] - half
    bot_p["split"] = "bottom"; bot_p["split_origin"] = origin
    return split_recursive(top_p, max_h, warnings) + split_recursive(bot_p, max_h, warnings)

def group_by_height(sections, max_h, warnings):
    groups = []; current = []; current_h = 0
    for s in sections:
        if s["h"] > max_h:
            if current: groups.append(current); current = []; current_h = 0
            for piece in split_recursive(s, max_h, warnings):
                groups.append([piece])
            continue
        if current_h + s["h"] > max_h and current:
            groups.append(current); current = []; current_h = 0
        current.append(s); current_h += s["h"]
    if current: groups.append(current)
    return groups

def export_coupang(file_key, coupang_id, coupang_prefix, token, warnings):
    url = "https://api.figma.com/v1/files/" + file_key + "/nodes?ids=" + coupang_id + "&depth=1"
    r   = requests.get(url, headers={"X-Figma-Token": token}); r.raise_for_status()
    parent   = r.json()["nodes"][coupang_id]["document"]
    parent_y = parent["absoluteBoundingBox"]["y"]
    parent_h = parent["absoluteBoundingBox"]["height"]

    sections = []
    for child in parent.get("children", []):
        bb    = child.get("absoluteBoundingBox", {})
        y_abs = bb.get("y", 0); h = bb.get("height", 0)
        sections.append({"id": child["id"], "name": child["name"],
                         "y_abs": y_abs, "y_rel": y_abs - parent_y, "h": h})
    sections.sort(key=lambda s: s["y_abs"])
    groups = group_by_height(sections, COUPANG_MAX_H, warnings)

    img_urls = get_image_urls(file_key, {coupang_id: "full"}, "jpg", token)
    full_url = get_url(img_urls, coupang_id)
    if not full_url:
        raise Exception("쿠팡 부모 프레임 URL 없음")

    r2       = requests.get(full_url); r2.raise_for_status()
    full_img = Image.open(io.BytesIO(r2.content)).convert("RGB")
    img_w, img_h = full_img.size
    scale    = img_h / parent_h

    results = {}
    for i, g in enumerate(groups):
        num      = i + 1
        y_top    = min(s["y_rel"] for s in g)
        y_bottom = max(s["y_rel"] + s["h"] for s in g)
        top      = max(0, int(y_top * scale))
        bottom   = min(img_h, int(y_bottom * scale))
        fname    = coupang_prefix + "(780)_쿠팡_" + str(num).zfill(2)
        data     = compress_jpg(full_img.crop((0, top, img_w, bottom)), COUPANG_MAX_MB)
        results[fname] = data
    return results

def make_zip(thumb_results, detail_results, coupang_results):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, data in thumb_results.items():
            zf.writestr("썸네일_PNG/" + fname + ".png", data)
        for fname, data in detail_results.items():
            zf.writestr("상세페이지_JPG/" + fname + ".jpg", data)
        for fname, data in coupang_results.items():
            zf.writestr("쿠팡_JPG/" + fname + ".jpg", data)
    return buf.getvalue()

# Streamlit UI
st.set_page_config(page_title="Figma 온라인상세 내보내기", page_icon="🎨", layout="centered")
st.title("🎨 Figma 온라인상세 내보내기")
st.caption("어린이과학동아 · 어린이수학동아 · 과학동아 | v1.1")
st.divider()

st.subheader("① Figma 토큰")
token = st.text_input("token", type="password", placeholder="figd_xxxxxxxxxxxxxxxx", label_visibility="collapsed")
st.divider()

st.subheader("② Figma 주소")
figma_url = st.text_input("url", placeholder="https://www.figma.com/design/...?node-id=...", label_visibility="collapsed")
st.divider()

run = st.button("🚀 내보내기 실행", type="primary", use_container_width=True)

if run:
    if not token:
        st.error("Figma 토큰을 입력하세요.")
        st.stop()
    if not figma_url:
        st.error("Figma 주소를 입력하세요.")
        st.stop()

    url        = unquote(figma_url.strip())
    key_match  = re.search(r"/(?:design|file)/([a-zA-Z0-9_-]+)", url)
    node_match = re.search(r"node-id=([0-9]+)[%\-]([0-9]+)", url)

    if not key_match:
        st.error("URL에서 파일 키를 찾을 수 없습니다.")
        st.stop()
    if not node_match:
        st.error("URL에서 node-id를 찾을 수 없습니다. 캔버스를 선택 후 URL을 복사하세요.")
        st.stop()

    file_key  = key_match.group(1)
    node_id   = node_match.group(1) + ":" + node_match.group(2)
    warnings  = []
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    status    = st.empty()
    bar       = st.progress(0)

    try:
        status.info("🔍 노드 구조 탐색 중...")
        bar.progress(5)
        thumb_nodes, detail_nodes, coupang_id, coupang_prefix = discover_nodes(file_key, node_id, token)
        status.info("✅ 탐색 완료 — 썸네일 " + str(len(thumb_nodes)) + "개 · 상세 " + str(len(detail_nodes)) + "개")

        bar.progress(10)
        status.info("🖼️ 썸네일 PNG 다운로드 중... (" + str(len(thumb_nodes)) + "개)")
        thumb_results = download_nodes_parallel(file_key, thumb_nodes, "png", token)
        bar.progress(40)

        status.info("📄 상세페이지 JPG 다운로드 중... (" + str(len(detail_nodes)) + "개)")
        detail_results = download_nodes_parallel(file_key, detail_nodes, "jpg", token)
        bar.progress(75)

        status.info("🛒 쿠팡 이미지 처리 중...")
        coupang_results = export_coupang(file_key, coupang_id, coupang_prefix, token, warnings)
        bar.progress(95)

        status.info("📦 ZIP 파일 생성 중...")
        zip_data = make_zip(thumb_results, detail_results, coupang_results)
        bar.progress(100)
        status.success("✅ 완료!")

        if warnings:
            with st.expander("⚠️ 경고 메시지", expanded=True):
                for w in warnings:
                    st.warning(w)

        st.divider()
        col1, col2, col3 = st.columns(3)
        col1.metric("썸네일_PNG",     str(len(thumb_results)) + "장")
        col2.metric("상세페이지_JPG", str(len(detail_results)) + "장")
        col3.metric("쿠팡_JPG",       str(len(coupang_results)) + "장")

        st.download_button(
            label="📥 ZIP 다운로드",
            data=zip_data,
            file_name="figma_output_" + timestamp + ".zip",
            mime="application/zip",
            use_container_width=True,
            type="primary"
        )

    except Exception as e:
        bar.progress(0)
        import traceback
        status.error("❌ 오류: " + str(e))
        st.code(traceback.format_exc())

with st.expander("📖 사용 방법"):
    st.markdown("""
1. **Figma 토큰** 입력 (figd_로 시작하는 Personal Access Token)
2. **Figma 캔버스** 선택 후 브라우저 URL 전체 복사
3. **실행** 버튼 클릭
4. 완료 후 **ZIP 다운로드** 버튼 클릭

**지원 잡지:** 어린이과학동아 · 어린이수학동아 · 과학동아
**출력 구조:** 썸네일_PNG / 상세페이지_JPG / 쿠팡_JPG (자동 분할)
    """)
