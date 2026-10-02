import os
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from supabase import Client, create_client

# ==========================================
# 1. ตั้งค่าการเชื่อมต่อ Supabase
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get(
    "SUPABASE_KEY"
)

if not SUPABASE_URL or not SUPABASE_KEY:
    print(
        "❌ Error: กรุณาตั้งค่า SUPABASE_URL และ SUPABASE_KEY ใน Environment Variables"
    )
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}


def get_gpf_nav_direct():
    nav_map = {}
    print("📡 กำลังดึงข้อมูล NAV จากเว็บ กบข. (GPF)...")
    try:
        gpf_url = "https://www.gpf.or.th/thai2019/About/main.php?page=memberfund&lang=th&size=n&pattern=n&menu=statistic"
        res = requests.get(gpf_url, headers=HEADERS, timeout=20)
        
        # 🟢 แก้ไขเรื่อง Encoding: กำหนด Encoding ก่อนจะดึง res.text
        res.encoding = res.apparent_encoding or "tis-620"

        soup = BeautifulSoup(res.text, "html.parser")
        for row in soup.find_all("tr"):
            text = row.get_text()
            cols = [
                re.sub(r"\s+", "", col.get_text())
                for col in row.find_all(["td", "th"])
            ]
            if not cols:
                continue

            nav_candidates = []
            for col_text in cols:
                # แมตช์ตัวเลขทศนิยม 4 ตำแหน่ง (รูปแบบ NAV ของ กบข.)
                match = re.search(r"^\d{1,3}(?:,\d{3})*\.\d{4}$", col_text)
                if match:
                    nav_candidates.append(
                        float(match.group(0).replace(",", ""))
                    )

            if not nav_candidates:
                continue

            nav_val = nav_candidates[0]

            # 🟢 จับคู่ตามคีย์เวิร์ด ID แผน และ Code สำรอง (GLOBAL, SET50, PROP)
            if "หุ้นต่างประเทศ" in text or "1788632129596" in text:
                nav_map["1788632129596"] = nav_val
                nav_map["แผนหุ้นต่างประเทศ"] = nav_val
            elif "หุ้นไทย" in text and "ต่างประเทศ" not in text:
                nav_map["1788631314182"] = nav_val
                nav_map["แผนหุ้นไทย"] = nav_val
            elif "อสังหาริมทรัพย์" in text or "1788632247228" in text:
                nav_map["1788632247228"] = nav_val
                nav_map["แผนอสังหาริมทรัพย์ไทย"] = nav_val
            elif "ตราสารหนี้" in text:
                nav_map["แผนตราสารหนี้"] = nav_val

    except Exception as e:
        print(f"⚠️ GPF Fetch Error: {e}")

    return nav_map


def run_gpf_update():
    thai_tz = timezone(timedelta(hours=7))
    now_thai_dt = datetime.now(thai_tz)
    now_thai_iso = now_thai_dt.isoformat()
    today_date_str = now_thai_dt.strftime("%Y-%m-%d")

    gpf_nav_data = get_gpf_nav_direct()
    if not gpf_nav_data:
        print(
            "⚠️ ไม่สามารถดึงข้อมูล NAV จาก GPF ได้ในรอบนี้ (ข้ามการอัปเดตเพื่อรักษาข้อมูลเดิม)"
        )
        return

    try:
        db_res = (
            supabase.table("portfolio_plans")
            .select("*")
            .ilike("app_source", "GPF")
            .execute()
        )
        gpf_items = db_res.data or []
    except Exception as e:
        print(f"❌ ไม่สามารถดึงข้อมูลจาก Supabase ได้: {e}")
        return

    print(f"📦 พบรายการ GPF ในระบบ {len(gpf_items)} รายการ")

    batch_payload = []
    for item in gpf_items:
        code = (
            item.get("asset_code", "").strip() if item.get("asset_code") else ""
        )
        name = (
            item.get("asset_name", "").strip() if item.get("asset_name") else ""
        )
        units = float(item.get("units") or 0)
        old_nav_date = item.get("nav_date")
        old_current_nav = item.get("current_nav")

        # ดึง NAV โดยลำดับการค้นหาจาก name ก่อน code
        latest_nav = gpf_nav_data.get(name) or gpf_nav_data.get(code)

        if latest_nav and latest_nav > 0:
            updated_item = item.copy()

            # 🟢 หากวันที่เปลี่ยนเป็นวันใหม่ ให้สลับ current_nav เก่า ไปไว้ที่ prev_nav เพื่อใช้คิดกำไรรายวัน
            prev_nav = item.get("prev_nav")
            if old_nav_date and old_nav_date != today_date_str and old_current_nav:
                prev_nav = old_current_nav

            updated_item.update(
                {
                    "prev_nav": round(float(prev_nav), 4) if prev_nav else round(latest_nav, 4),
                    "current_nav": round(latest_nav, 4),
                    "value": round(units * latest_nav, 4),
                    "nav_date": today_date_str,
                    "updated_at": now_thai_iso,
                }
            )
            batch_payload.append(updated_item)
            print(
                f" ✅ [GPF] {code or name}: NAV={latest_nav} | Value=฿{units * latest_nav:,.2f}"
            )
        else:
            print(
                f"⚠️ ไม่พบ NAV ของ [{code or name}] ในข้อมูลที่ดึงได้จากเว็บ กบข."
            )

    if batch_payload:
        try:
            supabase.table("portfolio_plans").upsert(batch_payload).execute()
            print(
                f"💾 อัปเดต Supabase แบบ Batch สำเร็จทั้งหมด {len(batch_payload)} รายการ"
            )
        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดในการอัปเดตแบบ Batch: {e}")
    else:
        print("⚠️ ไม่พบข้อมูลแผนลงทุน GPF ใน Supabase ที่จับคู่ตรงกัน")


if __name__ == "__main__":
    print("🚀 เริ่มต้นกระบวนการ Auto Update NAV (GPF + Batch Upsert)...")
    run_gpf_update()
    print("✨ ทำงานเสร็จสิ้น!")
