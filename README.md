# Figma 온라인상세 내보내기

Figma에 작업된 온라인 상세페이지 디자인을 각 판매 플랫폼에 맞는 이미지 파일로 자동 내보내기하는 웹 도구입니다.

## 지원 잡지
- 어린이과학동아
- 어린이수학동아
- 과학동아

## 출력 결과물
| 폴더 | 내용 |
|---|---|
| 썸네일_PNG | ds스토어, 예스24, 교보, 알라딘, wh 시리즈 등 9장 |
| 상세페이지_JPG | 예스24(940), 교보(900), DS(880), 네이버(860), 알라딘(700) 5장 |
| 쿠팡_JPG | 높이 3000px / 1MB 이하 자동 분할 (3~5장) |

## 로컬 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Cloud 배포

1. 이 저장소를 GitHub에 push
2. [share.streamlit.io](https://share.streamlit.io) 접속
3. 저장소 연결 → `app.py` 선택 → Deploy
