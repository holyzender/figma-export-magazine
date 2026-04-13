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
PX_TO_PLATFORM = {
    "940": "\uc608\uc2a4\ub85424",
    "900": "\uad50\ubcf4",
    "880": "DS",
    "860": "\ub124\uc774\ubc84",
    "700": "\uc54c\ub77c\ub518",
}
THUMB_MAP = {
    "ds\uc2a4\ud1a01000", "wh1000", "wh900", "wh600", "wh500", "wh458",
    "\uc54c\ub77c\ub518_w900", "\uc608\uc2a4\ub85424_h600", "\uad50\ubcf4_w458"
}


def api_get(path, token):
    r = requests.get(
        "https://api.figma.com/v1" + path,
        headers={"X-Figma-Token": token}
    )
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
    data = api_get("/files/" + file_key + "/nodes?ids=" + node_id + "&depth=2", token)
    canvas = data["nodes"][node_id]["document"]
    children = canvas.get("children", [])

    # SECTION 타입만 추출
    secs = [c for c in children if c.get("type") == "SECTION"]
    if len(secs) < 2:
        raise Exception(
            "\uc139\uc158\uc774 2\uac1c \uc774\uc0c1 \ud544\uc694\ud569\ub2c8\ub2e4. "
            "\ubc1c\uacac: " + str([c["name"] for c in children])
        )

    # 모든 섹션을 depth=3으로 가져오기
    sec_ids = ",".join(s["id"] for s in secs)
    td = api_get(
        "/files/" + file_key + "/nodes?ids=" + sec_ids + "&depth=3",
        token
    )

    thumb_sec_id = None
    detail_sec_id = None

    for sec in secs:
        sid = sec["id"]
        doc = td["nodes"][sid]["document"]
        found = False
        for child in doc.get("children", []):
            for sub in (child.get("children") or []):
                if sub.get("name") in THUMB_MAP:
                    found = True
                    break
            if found:
                break
        if found:
            thumb_sec_id = sid
        else:
            detail_sec_id = sid

    # fallback
    if not thumb_sec_id:
        thumb_sec_id = secs[0]["id"]
        detail_sec_id = secs[1]["id"]

    # 썸네일 노드
    thumb_nodes = {}
    thumb_doc = td["nodes"][thumb_sec_id]["document"]
    for child in thumb_doc.get("children", []):
        for sub in (child.get("children") or []):
            if sub.get("name") in THUMB_MAP:
                thumb_nodes[sub["id"]] = sub["name"]

    # 상세 노드
    detail_nodes = {}
    coupang_parent = None
    coupang_prefix = ""
    detail_doc = td["nodes"][detail_sec_id]["document"]
    for c in detail_doc.get("children", []):
        name = c["name"]
        if "780" in name and c["type"] in ("INSTANCE", "FRAME", "COMPONENT"):
            coupang_parent = c["id"]
            m = re.match(r"^(.+?)\(780\)", name)
            if m:
                coupang_prefix = m.group(1)
            continue
        m = re.match(r"^(.+?)\((\d+)\)", name)
        if m:
            px = m.group(2)
            platform = PX_TO_PLATFORM.get(px)
            if platform and c["type"] in ("INSTANCE", "FRAME", "COMPONENT"):
                detail_nodes[c["id"]] = m.group(1) + "(" + px + ")_" + platform

    if not thumb_nodes:
        raise Exception("\uc378\ub124\uc77c \ub178\ub4dc\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
    if not detail_nodes:
        raise Exception("\uc0c1\uc138\ud398\uc774\uc9c0 \ub178\ub4dc\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
    if not coupang_parent:
        raise Exception("\ucfe0\ud321 \ubd80\ubaa8 \ub178\ub4dc(780)\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")

    return thumb_nodes, detail_nodes, coupang_parent, coupang_prefix


def compress_jpg(img, max_size_mb=None):
    quality = 95
    while quality >= 10:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if max_size_mb is None or buf.tell() / (1024 * 1024) <= max_size_mb:
            return buf.getvalue()
        quality -= 5
    return buf.getvalue()


def download_nodes_parallel(file_key, node_dict, fmt, token):
    image_urls = get_image_urls(file_key, node_dict, fmt, token)
    results = {}

    def fetch_one(args):
        nid, fname = args
        url = get_url(image_urls, nid)
        if not url:
            return fname, None
        r = requests.get(url)
        r.raise_for_status()
        if fmt == "jpg":
            data = compress_jpg(
                Image.open(io.BytesIO(r.content)).convert("RGB"),
                COUPANG_MAX_MB
            )
        else:
            data = r.content
        return fname, data

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fetch_one, (nid, fname)): fname
            for nid, fname in node_dict.items()
        }
        for f in as_completed(futures):
            fname, data = f.result()
            if data:
                results[fname] = data
    return results


def split_recursive(s, max_h, warnings):
    if s["h"] <= max_h:
        return [s]
    if not s.get("split"):
        warnings.append(
            "[\uac15\uc81c\ubd84\ud560] '" + s["name"] + "' "
            + str(int(s["h"])) + "px > " + str(max_h) + "px"
        )
    half = s["h"] / 2.0
    origin = s.get("split_origin", s["name"])
    top_p = dict(s)
    top_p["h"] = half
    top_p["split"] = "top"
    top_p["split_origin"] = origin
    bot_p = dict(s)
    bot_p["y_rel"] = s["y_rel"] + half
    bot_p["h"] = s["h"] - half
    bot_p["split"] = "bottom"
    bot_p["split_origin"] = origin
    return split_recursive(top_p, max_h, warnings) + split_recursive(bot_p, max_h, warnings)


def group_by_height(sections, max_h, warnings):
    groups = []
    current = []
    current_h = 0
    for s in sections:
        if s["h"] > max_h:
            if current:
                groups.append(current)
                current = []
                current_h = 0
            for piece in split_recursive(s, max_h, warnings):
                groups.append([piece])
            continue
        if current_h + s["h"] > max_h and current:
            groups.append(current)
            current = []
            current_h = 0
        current.append(s)
        current_h += s["h"]
    if current:
        groups.append(current)
    return groups


def export_coupang(file_key, coupang_id, coupang_prefix, token, warnings):
    url = (
        "https://api.figma.com/v1/files/" + file_key
        + "/nodes?ids=" + coupang_id + "&depth=1"
    )
    r = requests.get(url, headers={"X-Figma-Token": token})
    r.raise_for_status()
    parent = r.json()["nodes"][coupang_id]["document"]
    parent_y = parent["absoluteBoundingBox"]["y"]
    parent_h = parent["absoluteBoundingBox"]["height"]

    sections = []
    for child in parent.get("children", []):
        bb = child.get("absoluteBoundingBox", {})
        y_abs = bb.get("y", 0)
        h = bb.get("height", 0)
        sections.append({
            "id": child["id"],
            "name": child["name"],
            "y_abs": y_abs,
            "y_rel": y_abs - parent_y,
            "h": h,
        })
    sections.sort(key=lambda s: s["y_abs"])
    groups = group_by_height(sections, COUPANG_MAX_H, warnings)

    img_urls = get_image_urls(file_key, {coupang_id: "full"}, "jpg", token)
    full_url = get_url(img_urls, coupang_id)
    if not full_url:
        raise Exception("\ucfe0\ud321 \ubd80\ubaa8 \ud504\ub808\uc784 URL \uc5c6\uc74c")

    r2 = requests.get(full_url)
    r2.raise_for_status()
    full_img = Image.open(io.BytesIO(r2.content)).convert("RGB")
    img_w, img_h = full_img.size
    scale = img_h / parent_h

    results = {}
    for i, g in enumerate(groups):
        num = i + 1
        y_top = min(s["y_rel"] for s in g)
        y_bottom = max(s["y_rel"] + s["h"] for s in g)
        top = max(0, int(y_top * scale))
        bottom = min(img_h, int(y_bottom * scale))
        fname = coupang_prefix + "(780)_\ucfe0\ud321_" + str(num).zfill(2)
        data = compress_jpg(full_img.crop((0, top, img_w, bottom)), COUPANG_MAX_MB)
        results[fname] = data
    return results


def make_zip(thumb_results, detail_results, coupang_results):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, data in thumb_results.items():
            zf.writestr("\uc378\ub124\uc77c_PNG/" + fname + ".png", data)
        for fname, data in detail_results.items():
            zf.writestr("\uc0c1\uc138\ud398\uc774\uc9c0_JPG/" + fname + ".jpg", data)
        for fname, data in coupang_results.items():
            zf.writestr("\ucfe0\ud321_JPG/" + fname + ".jpg", data)
    return buf.getvalue()


# ── Streamlit UI ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Figma \uc628\ub77c\uc778\uc0c1\uc138 \ub0b4\ubcf4\ub0b4\uae30",
    page_icon="\U0001f3a8",
    layout="centered",
)

st.title("\U0001f3a8 Figma \uc628\ub77c\uc778\uc0c1\uc138 \ub0b4\ubcf4\ub0b4\uae30")
st.caption("\uc5b4\ub9b0\uc774\uacfc\ud559\ub3d9\uc544 \xb7 \uc5b4\ub9b0\uc774\uc218\ud559\ub3d9\uc544 \xb7 \uacfc\ud559\ub3d9\uc544 | v1.2")
st.divider()

st.subheader("\u2460 Figma \ud1a0\ud070")
token = st.text_input(
    "token",
    type="password",
    placeholder="figd_xxxxxxxxxxxxxxxx",
    label_visibility="collapsed",
)
st.divider()

st.subheader("\u2461 Figma \uc8fc\uc18c")
figma_url = st.text_input(
    "url",
    placeholder="https://www.figma.com/design/...?node-id=...",
    label_visibility="collapsed",
)
st.divider()

run = st.button(
    "\U0001f680 \ub0b4\ubcf4\ub0b4\uae30 \uc2e4\ud589",
    type="primary",
    use_container_width=True,
)

if run:
    if not token:
        st.error("Figma \ud1a0\ud070\uc744 \uc785\ub825\ud558\uc138\uc694.")
        st.stop()
    if not figma_url:
        st.error("Figma \uc8fc\uc18c\ub97c \uc785\ub825\ud558\uc138\uc694.")
        st.stop()

    url = unquote(figma_url.strip())
    key_match = re.search(r"/(?:design|file)/([a-zA-Z0-9_-]+)", url)
    node_match = re.search(r"node-id=([0-9]+)[%\-]([0-9]+)", url)

    if not key_match:
        st.error("URL\uc5d0\uc11c \ud30c\uc77c \ud0a4\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
        st.stop()
    if not node_match:
        st.error("URL\uc5d0\uc11c node-id\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
        st.stop()

    file_key = key_match.group(1)
    node_id = node_match.group(1) + ":" + node_match.group(2)
    warnings = []
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    status = st.empty()
    bar = st.progress(0)

    try:
        status.info("\U0001f50d \ub178\ub4dc \ud0d0\uc0c9 \uc911...")
        bar.progress(5)
        thumb_nodes, detail_nodes, coupang_id, coupang_prefix = discover_nodes(
            file_key, node_id, token
        )
        status.info(
            "\u2705 \ud0d0\uc0c9 \uc644\ub8cc \u2014 \uc378\ub124\uc77c "
            + str(len(thumb_nodes)) + "\uac1c \xb7 \uc0c1\uc138 "
            + str(len(detail_nodes)) + "\uac1c"
        )

        bar.progress(10)
        status.info(
            "\U0001f5bc\ufe0f \uc378\ub124\uc77c PNG \ub2e4\uc6b4\ub85c\ub4dc \uc911... ("
            + str(len(thumb_nodes)) + "\uac1c)"
        )
        thumb_results = download_nodes_parallel(file_key, thumb_nodes, "png", token)
        bar.progress(40)

        status.info(
            "\U0001f4c4 \uc0c1\uc138\ud398\uc774\uc9c0 JPG \ub2e4\uc6b4\ub85c\ub4dc \uc911... ("
            + str(len(detail_nodes)) + "\uac1c)"
        )
        detail_results = download_nodes_parallel(file_key, detail_nodes, "jpg", token)
        bar.progress(75)

        status.info("\U0001f6d2 \ucfe0\ud321 \uc774\ubbf8\uc9c0 \ucc98\ub9ac \uc911...")
        coupang_results = export_coupang(
            file_key, coupang_id, coupang_prefix, token, warnings
        )
        bar.progress(95)

        status.info("\U0001f4e6 ZIP \ud30c\uc77c \uc0dd\uc131 \uc911...")
        zip_data = make_zip(thumb_results, detail_results, coupang_results)
        bar.progress(100)
        status.success("\u2705 \uc644\ub8cc!")

        if warnings:
            with st.expander("\u26a0\ufe0f \uacbd\uace0 \uba54\uc2dc\uc9c0", expanded=True):
                for w in warnings:
                    st.warning(w)

        st.divider()
        col1, col2, col3 = st.columns(3)
        col1.metric("\uc378\ub124\uc77c_PNG", str(len(thumb_results)) + "\uc7a5")
        col2.metric("\uc0c1\uc138\ud398\uc774\uc9c0_JPG", str(len(detail_results)) + "\uc7a5")
        col3.metric("\ucfe0\ud321_JPG", str(len(coupang_results)) + "\uc7a5")

        st.download_button(
            label="\U0001f4e5 ZIP \ub2e4\uc6b4\ub85c\ub4dc",
            data=zip_data,
            file_name="figma_output_" + timestamp + ".zip",
            mime="application/zip",
            use_container_width=True,
            type="primary",
        )

    except Exception as e:
        bar.progress(0)
        import traceback
        status.error("\u274c \uc624\ub958: " + str(e))
        st.code(traceback.format_exc())

with st.expander("\U0001f4d6 \uc0ac\uc6a9 \ubc29\ubc95"):
    st.markdown("""
1. **Figma \ud1a0\ud070** \uc785\ub825 (figd_\ub85c \uc2dc\uc791\ud558\ub294 Personal Access Token)
2. **Figma \ucee8\ubc84\uc2a4** \uc120\ud0dd \ud6c4 \ube0c\ub77c\uc6b0\uc800 URL \uc804\uccb4 \ubcf5\uc0ac
3. **\uc2e4\ud589** \ubc84\ud2bc \ud074\ub9ad
4. \uc644\ub8cc \ud6c4 **ZIP \ub2e4\uc6b4\ub85c\ub4dc** \ubc84\ud2bc \ud074\ub9ad

**\uc9c0\uc6d0 \uc7a1\uc9c0:** \uc5b4\ub9b0\uc774\uacfc\ud559\ub3d9\uc544 \xb7 \uc5b4\ub9b0\uc774\uc218\ud559\ub3d9\uc544 \xb7 \uacfc\ud559\ub3d9\uc544
**\ucd9c\ub825 \uad6c\uc870:** \uc378\ub124\uc77c_PNG / \uc0c1\uc138\ud398\uc774\uc9c0_JPG / \ucfe0\ud321_JPG (\uc790\ub3d9 \ubd84\ud560)
    """)
