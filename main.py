from __future__ import annotations

import html
import json
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import requests
from icalendar import Calendar


# =========================================================
# 基本設定
# =========================================================

TIMEZONE = ZoneInfo("Asia/Tokyo")
OUTPUT_PATH = Path("output/morning_radio.txt")

# 取得するニュース件数
MAX_ARTICLES_PER_FEED = 5
MAX_TOTAL_ARTICLES = 24

# Gemini API
# 無料枠が利用できるモデル名を環境変数で変更できます。
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

# RSS一覧
# 配信元の都合でURLが変更・廃止される場合があります。
RSS_FEEDS = [
    {
        "category": "国内ニュース",
        "name": "NHK主要ニュース",
        "url": "https://www.nhk.or.jp/rss/news/cat0.xml",
    },
    {
        "category": "世界ニュース",
        "name": "BBC World",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "category": "AIニュース",
        "name": "OpenAI News",
        "url": "https://openai.com/news/rss.xml",
    },
    {
        "category": "AIニュース",
        "name": "Google AI Blog",
        "url": "https://blog.google/technology/ai/rss/",
    },
    {
        "category": "テクノロジー",
        "name": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
    },
    {
        "category": "Appleニュース",
        "name": "Apple Newsroom",
        "url": "https://www.apple.com/newsroom/rss-feed.rss",
    },
    {
        "category": "DTM・音楽制作",
        "name": "MusicRadar",
        "url": "https://www.musicradar.com/feeds/all",
    },
]


# =========================================================
# 共通処理
# =========================================================

def clean_text(value: Any, max_length: int = 500) -> str:
    """HTMLタグや余分な空白を除去します。"""
    if value is None:
        return ""

    text = str(value)
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_length:
        return text[:max_length].rstrip() + "…"

    return text


def get_japanese_weekday(target_date: date) -> str:
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    return weekdays[target_date.weekday()]


def ensure_aware_datetime(value: datetime) -> datetime:
    """タイムゾーンのない日時を日本時間として扱います。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


# =========================================================
# Googleカレンダー
# =========================================================

def get_calendar_events(target_date: date) -> list[dict[str, str]]:
    """
    Googleカレンダーの非公開iCal URLから対象日の予定を取得します。

    GitHub Secrets:
        GOOGLE_CALENDAR_ICAL_URL
    """
    calendar_url = os.getenv("GOOGLE_CALENDAR_ICAL_URL", "").strip()

    if not calendar_url:
        print("GOOGLE_CALENDAR_ICAL_URLが未設定です。予定なしとして続行します。")
        return []

    try:
        response = requests.get(calendar_url, timeout=30)
        response.raise_for_status()
        calendar = Calendar.from_ical(response.content)
    except Exception as exc:
        print(f"カレンダー取得エラー: {exc}")
        return []

    day_start = datetime.combine(target_date, time.min, tzinfo=TIMEZONE)
    day_end = day_start + timedelta(days=1)

    events: list[dict[str, str]] = []

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        try:
            start_value = component.decoded("DTSTART")
            end_value = component.decoded("DTEND", default=None)
            title = clean_text(component.get("SUMMARY", "名称未設定"), 150)
            location = clean_text(component.get("LOCATION", ""), 150)

            # 終日予定
            if isinstance(start_value, date) and not isinstance(start_value, datetime):
                event_date = start_value

                # DTENDは終端を含まないため、開始日の判定を優先
                if event_date == target_date:
                    events.append(
                        {
                            "time": "終日",
                            "title": title,
                            "location": location,
                        }
                    )
                continue

            if not isinstance(start_value, datetime):
                continue

            start_dt = ensure_aware_datetime(start_value)

            if isinstance(end_value, datetime):
                end_dt = ensure_aware_datetime(end_value)
            else:
                end_dt = start_dt + timedelta(hours=1)

            # 対象日と重なる予定を抽出
            if start_dt < day_end and end_dt > day_start:
                events.append(
                    {
                        "time": start_dt.strftime("%H:%M"),
                        "title": title,
                        "location": location,
                    }
                )

        except Exception as exc:
            print(f"予定の解析をスキップしました: {exc}")

    events.sort(
        key=lambda event: (
            event["time"] == "終日",
            event["time"],
            event["title"],
        )
    )

    return events


def format_calendar_events(events: list[dict[str, str]]) -> str:
    if not events:
        return "本日の登録予定はありません。"

    lines = []

    for event in events:
        location_text = (
            f"、場所は{event['location']}" if event.get("location") else ""
        )
        lines.append(
            f"・{event['time']}：{event['title']}{location_text}"
        )

    return "\n".join(lines)


# =========================================================
# RSSニュース
# =========================================================

def parse_entry_datetime(entry: Any) -> datetime | None:
    """RSS記事の日付を可能な範囲で読み取ります。"""
    for attribute in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attribute, None)

        if parsed:
            try:
                return datetime(
                    parsed.tm_year,
                    parsed.tm_mon,
                    parsed.tm_mday,
                    parsed.tm_hour,
                    parsed.tm_min,
                    parsed.tm_sec,
                    tzinfo=ZoneInfo("UTC"),
                ).astimezone(TIMEZONE)
            except Exception:
                continue

    return None


def get_news() -> list[dict[str, str]]:
    articles: list[dict[str, str]] = []

    for feed_config in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_config["url"])

            if getattr(feed, "bozo", False):
                print(
                    f"RSS警告: {feed_config['name']} "
                    f"{getattr(feed, 'bozo_exception', '')}"
                )

            for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
                title = clean_text(entry.get("title", "タイトル不明"), 200)
                summary = clean_text(
                    entry.get("summary", entry.get("description", "")),
                    450,
                )
                link = clean_text(entry.get("link", ""), 500)
                published_dt = parse_entry_datetime(entry)

                articles.append(
                    {
                        "category": feed_config["category"],
                        "source": feed_config["name"],
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "published": (
                            published_dt.isoformat()
                            if published_dt
                            else ""
                        ),
                    }
                )

        except Exception as exc:
            print(f"RSS取得エラー [{feed_config['name']}]: {exc}")

    # 同一タイトルを除去
    unique_articles: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    for article in articles:
        normalized_title = re.sub(
            r"\W+",
            "",
            article["title"].lower(),
        )

        if normalized_title in seen_titles:
            continue

        seen_titles.add(normalized_title)
        unique_articles.append(article)

    # 日時がある記事を新しい順にする
    unique_articles.sort(
        key=lambda article: article["published"],
        reverse=True,
    )

    return unique_articles[:MAX_TOTAL_ARTICLES]


def format_news_for_prompt(articles: list[dict[str, str]]) -> str:
    if not articles:
        return "ニュース記事を取得できませんでした。"

    lines = []

    for index, article in enumerate(articles, start=1):
        published = article["published"]

        if published:
            try:
                published_dt = datetime.fromisoformat(published)
                published_text = published_dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                published_text = published
        else:
            published_text = "日時不明"

        lines.extend(
            [
                f"[記事{index}]",
                f"カテゴリー: {article['category']}",
                f"媒体: {article['source']}",
                f"日時: {published_text}",
                f"見出し: {article['title']}",
                f"概要: {article['summary'] or '概要なし'}",
                f"URL: {article['link']}",
                "",
            ]
        )

    return "\n".join(lines)


# =========================================================
# Gemini API
# =========================================================

def build_prompt(
    target_date: date,
    calendar_text: str,
    news_text: str,
) -> str:
    weekday = get_japanese_weekday(target_date)

    return f"""
あなたは、日本語の朝のラジオ番組を担当する落ち着いたパーソナリティーです。
以下の予定とニュース資料だけを使い、約8〜10分で読み上げられる原稿を作成してください。

【日付】
{target_date.year}年{target_date.month}月{target_date.day}日（{weekday}曜日）

【今日の予定】
{calendar_text}

【取得したニュース資料】
{news_text}

【番組構成】
1. 短いオープニング
2. 今日の日付と予定の整理
3. 国内ニュース
4. AI・テクノロジーニュース
5. 世界ニュース
6. Apple関連ニュース
7. DTM・EDM・音楽制作関連ニュース
8. 今日の予定を踏まえた行動上の注意
9. 短いエンディング

【重要なルール】
・自然な日本語のラジオ原稿にしてください。
・見出しの羅列ではなく、話し言葉としてつないでください。
・ニュースごとに情報源を自然に伝えてください。
・資料にない内容を事実として追加しないでください。
・情報がないカテゴリーは、無理に内容を作らず省略してください。
・記事タイトルだけで断定せず、概要の範囲内で説明してください。
・同じ話題は重複させないでください。
・URLは読み上げ原稿に含めないでください。
・絵文字、Markdown記号、箇条書き、表は使わないでください。
・約3,000〜4,500文字を目安にしてください。
・最後に「それでは、今日も良い一日をお過ごしください。」と述べてください。

完成した読み上げ原稿だけを出力してください。
""".strip()


def generate_with_gemini(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEYが設定されていません。"
            "GitHub Secretsに登録してください。"
        )

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.45,
            "topP": 0.9,
            "maxOutputTokens": 8192,
        },
    }

    response = requests.post(
        endpoint,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False),
        timeout=180,
    )

    if not response.ok:
        raise RuntimeError(
            f"Gemini APIエラー: HTTP {response.status_code}\n"
            f"{response.text}"
        )

    data = response.json()

    try:
        candidates = data["candidates"]
        parts = candidates[0]["content"]["parts"]
        result = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict)
        ).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Gemini APIの応答を解析できませんでした。\n"
            f"{json.dumps(data, ensure_ascii=False, indent=2)}"
        ) from exc

    if not result:
        raise RuntimeError("Gemini APIから空の原稿が返されました。")

    return result


# =========================================================
# AIエラー時の予備原稿
# =========================================================

def create_fallback_script(
    target_date: date,
    calendar_text: str,
    articles: list[dict[str, str]],
) -> str:
    weekday = get_japanese_weekday(target_date)

    lines = [
        "おはようございます。",
        (
            f"今日は{target_date.year}年{target_date.month}月"
            f"{target_date.day}日、{weekday}曜日です。"
        ),
        "",
        "まず、今日の予定を確認します。",
        calendar_text,
        "",
        "続いて、取得したニュースの主な見出しをお伝えします。",
    ]

    for article in articles[:12]:
        lines.append(
            f"{article['source']}から、{article['title']}。"
        )

        if article["summary"]:
            lines.append(article["summary"])

    lines.extend(
        [
            "",
            "以上、今朝取得した情報をお伝えしました。",
            "それでは、今日も良い一日をお過ごしください。",
        ]
    )

    return "\n".join(lines)


# =========================================================
# 保存
# =========================================================

def save_script(script: str, target_date: date) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    header = (
        f"朝のラジオ原稿\n"
        f"生成日: {target_date.isoformat()}\n"
        f"生成時刻: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}\n"
        f"\n"
    )

    OUTPUT_PATH.write_text(
        header + script.strip() + "\n",
        encoding="utf-8",
    )


# =========================================================
# メイン処理
# =========================================================

def main() -> None:
    now = datetime.now(TIMEZONE)
    target_date = now.date()

    print(f"対象日: {target_date.isoformat()}")

    events = get_calendar_events(target_date)
    calendar_text = format_calendar_events(events)

    print(f"予定件数: {len(events)}")

    articles = get_news()
    news_text = format_news_for_prompt(articles)

    print(f"ニュース件数: {len(articles)}")

    prompt = build_prompt(
        target_date=target_date,
        calendar_text=calendar_text,
        news_text=news_text,
    )

    try:
        script = generate_with_gemini(prompt)
        print("Geminiによる原稿生成に成功しました。")
    except Exception as exc:
        print(f"AI原稿生成に失敗しました: {exc}")
        print("予備形式の原稿を生成します。")
        script = create_fallback_script(
            target_date,
            calendar_text,
            articles,
        )

    save_script(script, target_date)
    print(f"保存完了: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
