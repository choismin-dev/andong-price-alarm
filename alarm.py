import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

# =====================================================================
# [사용자 설정 영역] 이 부분만 원하시는 조건으로 수정하세요!
# =====================================================================
TARGET_CROPS = ["병마","둥근마", "원강","구강","재강"]          # 전체 품종을 받으려면 [] 로 두세요.
TARGET_GRADES = []  # 전체 등급을 다 받으려면 [] 로 두세요.

# 휴대폰 ntfy 앱에서 구독한 주제 이름 입력
NTFY_TOPIC = "andong_agri_price"

# 텔레그램을 함께/대신 쓰려면 아래 두 칸 입력 (안 쓰면 빈칸 유지)
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
# =====================================================================

KST = timezone(timedelta(hours=9))
GRADE_ORDER = {
    "특1": 1, "특2": 2, "특3": 3, "특4": 4, "특5": 5, "특": 9,
    "상1": 11, "상2": 12, "상3": 13, "상4": 14, "상5": 15, "상": 19,
    "보1": 21, "보2": 22, "보3": 23, "보4": 24, "보5": 25, "보": 29,
    "등외": 90
}
LAST_SENT_FILE = "last_sent_date.txt"


class AndongAlarmScraper:
    BASE_URL = "https://adnacf.co.kr/web/bbs/board.php"

    def __init__(self, bo_table="price2025"):
        self.bo_table = bo_table
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"
        })

    def parse_date_from_text(self, text):
        if not text:
            return None
        match = re.search(r"(20\d{2})\s*[년\-\.\/]?\s*(\d{1,2})\s*[월\-\.\/]?\s*(\d{1,2})\s*일?", text)
        if match:
            y, m, d = map(int, match.groups())
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{m:02d}-{d:02d}"
        return None

    def get_latest_post(self):
        resp = self.session.get(self.BASE_URL, params={"bo_table": self.bo_table, "page": 1}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        posts = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if f"bo_table={self.bo_table}" in href and "wr_id=" in href and "#c_" not in href and "w=" not in href:
                wr_id = parse_qs(urlparse(href).query).get("wr_id", [None])[0]
                if not wr_id or not wr_id.isdigit():
                    continue
                title = " ".join(a_tag.get_text().split())
                if not title or title in ["목록", "이전글", "다음글"]:
                    continue
                posts.append({
                    "wr_id": int(wr_id),
                    "title": title,
                    "date": self.parse_date_from_text(title),
                    "url": urljoin(self.BASE_URL, href)
                })
        return max(posts, key=lambda x: x["wr_id"]) if posts else None

    def expand_table_to_grid(self, table):
        grid = []
        rowspans = {}
        for r_idx, tr in enumerate(table.find_all("tr")):
            row_data = []
            c_idx = 0
            for cell in tr.find_all(["td", "th"]):
                while (r_idx, c_idx) in rowspans:
                    val, rem = rowspans.pop((r_idx, c_idx))
                    row_data.append(val)
                    if rem > 1:
                        rowspans[(r_idx + 1, c_idx)] = (val, rem - 1)
                    c_idx += 1
                text = " ".join(cell.get_text().split())
                r_span = int(cell.get("rowspan", 1))
                c_span = int(cell.get("colspan", 1))
                for _ in range(c_span):
                    row_data.append(text)
                    if r_span > 1:
                        rowspans[(r_idx + 1, c_idx)] = (text, r_span - 1)
                    c_idx += 1
            while (r_idx, c_idx) in rowspans:
                val, rem = rowspans.pop((r_idx, c_idx))
                row_data.append(val)
                if rem > 1:
                    rowspans[(r_idx + 1, c_idx)] = (val, rem - 1)
                c_idx += 1
            if any(row_data):
                grid.append(row_data)
        return grid

    def parse_post(self, post_url, title_date):
        resp = self.session.get(post_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        content_div = soup.find(id="bo_v_con") or soup

        content_text = " ".join(content_div.get_text().split())
        body_match = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일[^\n]{0,20}?시세표", content_text)
        if body_match:
            y, m, d = map(int, body_match.groups())
            actual_date = f"{y:04d}-{m:02d}-{d:02d}"
        else:
            actual_date = self.parse_date_from_text(content_text[:200]) or title_date

        tables = content_div.find_all("table")
        if not tables:
            return actual_date, []

        main_table = max(tables, key=lambda t: len(t.find_all("tr")))
        grid = self.expand_table_to_grid(main_table)

        header_idx = None
        headers = []
        for idx, row in enumerate(grid):
            row_str = re.sub(r"\s+", "", "".join(row))
            if ("품종" in row_str or "품목" in row_str) and ("평균" in row_str or "최고" in row_str):
                header_idx = idx
                headers = [re.sub(r"\s+", "", h) for h in row]
                break

        if header_idx is None:
            return actual_date, []

        col_count = len(headers)
        crop_idx = next((i for i, h in enumerate(headers) if "품종" in h), 0)
        grade_idx = next((i for i, h in enumerate(headers) if "등급" in h or "크기" in h), 1)
        qty_idx = next((i for i, h in enumerate(headers) if "수량" in h or "물량" in h), 3)
        high_idx = next((i for i, h in enumerate(headers) if "최고" in h), 4)
        low_idx = next((i for i, h in enumerate(headers) if "최저" in h), 5)
        avg_idx = next((i for i, h in enumerate(headers) if "평균" in h), 6)

        extracted = []
        last_crop = ""
        grade_pattern = re.compile(r"^(특|상|보)\d*$|^등외$")

        for row in grid[header_idx + 1:]:
            if len(row) < col_count - 1:
                continue
            row = ([""] + row) if len(row) == col_count - 1 else row[:col_count]

            crop_name = row[crop_idx].strip()
            clean_first = re.sub(r"\s+", "", crop_name)

            if grade_pattern.match(clean_first) and last_crop:
                row = [last_crop] + row[:col_count - 1]
                crop_name = last_crop
            elif not crop_name and last_crop:
                crop_name = last_crop
            else:
                last_crop = crop_name

            if not crop_name or "품종" in crop_name or "금일" in crop_name or "합계" in crop_name:
                continue
            if TARGET_CROPS and not any(t in crop_name for t in TARGET_CROPS):
                continue

            grade_name = re.sub(r"\s+", "", row[grade_idx].strip())
            if TARGET_GRADES and not any(g == grade_name for g in TARGET_GRADES):
                continue

            extracted.append({
                "품종명": crop_name,
                "등급": grade_name,
                "수량": row[qty_idx].strip(),
                "최고가": row[high_idx].strip(),
                "최저가": row[low_idx].strip(),
                "평균가": row[avg_idx].strip()
            })

        extracted.sort(key=lambda x: (x["품종명"], GRADE_ORDER.get(x["등급"], 50)))
        return actual_date, extracted


def send_notifications(message, date_str):
    if NTFY_TOPIC.strip():
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC.strip()}",
            data=message.encode("utf-8"),
            headers={"Title": f"Andong Market ({date_str})"},
            timeout=15
        )
        resp.raise_for_status()
        print(f"ntfy 알림 전송 완료: {date_str}")

    if TELEGRAM_BOT_TOKEN.strip() and TELEGRAM_CHAT_ID.strip():
        tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN.strip()}/sendMessage"
        resp = requests.post(
            tg_url,
            json={"chat_id": TELEGRAM_CHAT_ID.strip(), "text": message},
            timeout=15
        )
        resp.raise_for_status()
        print(f"텔레그램 알림 전송 완료: {date_str}")


def main():
    scraper = AndongAlarmScraper()
    post = scraper.get_latest_post()
    if not post:
        print("게시글 없음")
        return

    actual_date, rows = scraper.parse_post(post["url"], post["date"])
    post_key = f"{actual_date}_{post['wr_id']}"
    force_test = os.environ.get("FORCE_TEST", "").lower() == "true"

    if not force_test and os.path.exists(LAST_SENT_FILE):
        with open(LAST_SENT_FILE, "r", encoding="utf-8") as f:
            if f.read().strip() == post_key:
                print(f"이미 발송 완료된 시세표입니다: {post_key}")
                return

    lines = [f"🍎 [안동농협 공판장 시세 알림]", f"📅 경매일자: {actual_date}\n"]
    if not rows:
        crops_str = ", ".join(TARGET_CROPS) if TARGET_CROPS else "전체 품종"
        lines.append(f"금일 시세표에 지정하신 품종/등급({crops_str}) 거래 내역이 없습니다.")
    else:
        current_crop = None
        for r in rows:
            if r["품종명"] != current_crop:
                current_crop = r["품종명"]
                lines.append(f"■ {current_crop}")
            lines.append(f" · {r['등급']}: 평균 {r['평균가']}원 (최고 {r['최고가']} / 최저 {r['최저가']} / {r['수량']}상자)")

    lines.append(f"\n🔗 원본 시세표:\n{post['url']}")
    send_notifications("\n".join(lines), actual_date)

    with open(LAST_SENT_FILE, "w", encoding="utf-8") as f:
        f.write(post_key)


if __name__ == "__main__":
    main()
