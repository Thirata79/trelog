import os
import json
import tempfile
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

# ---------- クライアント設定 ----------
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# ---------- セッション一時保存（メモリ内） ----------
# { userId: { studentName, menu, memo, next } }
sessions = {}

# ---------- LINE送信ヘルパー ----------
def push_message(to, messages):
    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json"
    }
    requests.post(LINE_PUSH_URL, headers=headers, json={"to": to, "messages": messages})

def reply_message(reply_token, messages):
    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json"
    }
    requests.post(LINE_REPLY_URL, headers=headers, json={"replyToken": reply_token, "messages": messages})

# ---------- Webhook ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.json
    events = body.get("events", [])
    for event in events:
        event_type = event.get("type")
        user_id = event.get("source", {}).get("userId", "")
        reply_token = event.get("replyToken", "")

        print(f"[EVENT] type={event_type} user={user_id}", flush=True)

        try:
            if event_type == "message":
                msg = event.get("message", {})
                msg_type = msg.get("type")
                print(f"[MESSAGE] type={msg_type}", flush=True)
                if msg_type == "audio":
                    handle_audio(user_id, reply_token, msg.get("id"))
                elif msg_type == "text":
                    handle_text(user_id, reply_token, msg.get("text", ""))

            elif event_type == "postback":
                data = event.get("postback", {}).get("data", "")
                print(f"[POSTBACK] data={data}", flush=True)
                handle_postback(user_id, reply_token, data)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()

    return jsonify({"status": "ok"})

# ---------- 音声処理 ----------
def handle_audio(user_id, reply_token, message_id):
    # LINEから音声ダウンロード
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    res = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers=headers
    )

    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        f.write(res.content)
        audio_path = f.name

    # Whisper文字起こし
    with open(audio_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ja"
        )
    text = transcript.text

    # GPT解析 → 確認メッセージ
    parse_and_confirm(user_id, reply_token, text)

# ---------- GPT解析＋確認（音声・テキスト共通） ----------
def parse_and_confirm(user_id, reply_token, text):
    gpt_res = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant that extracts session notes from a Japanese fitness/martial arts trainer. "
                    "Always respond with valid JSON only, no markdown, no explanation. "
                    'Format: {"Student name":"name","Menu":"what was done","Memo":"observations","Next":"next steps"}'
                )
            },
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"}
    )

    data = json.loads(gpt_res.choices[0].message.content)
    student = data.get("Student name", "")
    menu = data.get("Menu", "")
    memo = data.get("Memo", "")
    next_session = data.get("Next", "")

    sessions[user_id] = {
        "studentName": student,
        "menu": menu,
        "memo": memo,
        "next": next_session
    }

    reply_message(reply_token, [
        {
            "type": "text",
            "text": (
                f"✅ 以下の内容で解析しました！\n\n"
                f"👤 生徒：{student or '（未確認）'}\n"
                f"📋 メニュー：{menu}\n"
                f"📝 メモ：{memo}\n"
                f"🔜 次回：{next_session}\n\n"
                "この内容で記録しますか？"
            ),
            "quickReply": {
                "items": [
                    {
                        "type": "action",
                        "action": {
                            "type": "postback",
                            "label": "📝 記録する",
                            "data": "action=記録"
                        }
                    },
                    {
                        "type": "action",
                        "action": {
                            "type": "postback",
                            "label": "✏️ やり直す",
                            "data": "action=retry"
                        }
                    }
                ]
            }
        }
    ])

# ---------- テキスト処理 ----------
def handle_text(user_id, reply_token, text):
    cmd = text.strip()

    # /記録 → 記録開始の案内
    if cmd in ["/記録", "記録"]:
        reply_message(reply_token, [
            {"type": "text", "text": "音声またはテキストで稽古内容を送ってください 🎤✏️"}
        ])

    # /送信 → 直前の記録を生徒に送信
    elif cmd in ["/送信", "送信"]:
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [
                {"type": "text", "text": "⚠️ 送信するデータがありません。先に記録してください。"}
            ])
            return
        student_name = session.get("studentName", "")
        student_line_id = get_student_line_id(student_name)
        if student_line_id:
            push_message(student_line_id, [
                {
                    "type": "text",
                    "text": (
                        f"【稽古記録】\n"
                        f"メニュー：{session.get('menu', '')}\n"
                        f"メモ：{session.get('memo', '')}\n"
                        f"次回：{session.get('next', '')}"
                    )
                }
            ])
            reply_message(reply_token, [
                {"type": "text", "text": f"✅ {student_name}さんに送信しました！"}
            ])
            sessions.pop(user_id, None)
        else:
            reply_message(reply_token, [
                {"type": "text", "text": f"⚠️ 「{student_name}」のLINE IDが登録されていません。"}
            ])

    # /準備 → 次回セッションの申し送り確認
    elif cmd in ["/準備", "準備"]:
        try:
            creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
            import re
            try:
                creds_data = json.loads(creds_json)
            except json.JSONDecodeError:
                fixed = re.sub(
                    r'("private_key"\s*:\s*")(.*?)(")',
                    lambda m: m.group(1) + m.group(2).replace('\n', '\\n') + m.group(3),
                    creds_json, flags=re.DOTALL
                )
                creds_data = json.loads(fixed)
            if "private_key" in creds_data:
                creds_data["private_key"] = creds_data["private_key"].replace("\\n", "\n")
            client = gspread.service_account_from_dict(creds_data)
            sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
            all_rows = sheet.get_all_values()
            # 最新5件の申し送りを表示（ヘッダー除く）
            recent = all_rows[-5:] if len(all_rows) > 5 else all_rows[1:]
            if recent:
                lines = []
                for row in reversed(recent):
                    name = row[2] if len(row) > 2 else ""
                    date = row[1] if len(row) > 1 else ""
                    next_note = row[6] if len(row) > 6 else ""
                    if next_note:
                        lines.append(f"📌 {name}（{date}）\n→ {next_note}")
                if lines:
                    reply_message(reply_token, [
                        {"type": "text", "text": "【次回への申し送り】\n\n" + "\n\n".join(lines)}
                    ])
                else:
                    reply_message(reply_token, [
                        {"type": "text", "text": "申し送りデータがありません。"}
                    ])
            else:
                reply_message(reply_token, [
                    {"type": "text", "text": "まだ記録がありません。"}
                ])
        except Exception as e:
            print(f"[準備エラー] {e}", flush=True)
            reply_message(reply_token, [
                {"type": "text", "text": "⚠️ データ取得中にエラーが発生しました。"}
            ])

    # /レポート → 直近の記録サマリー
    elif cmd in ["/レポート", "レポート"]:
        try:
            creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
            import re
            try:
                creds_data = json.loads(creds_json)
            except json.JSONDecodeError:
                fixed = re.sub(
                    r'("private_key"\s*:\s*")(.*?)(")',
                    lambda m: m.group(1) + m.group(2).replace('\n', '\\n') + m.group(3),
                    creds_json, flags=re.DOTALL
                )
                creds_data = json.loads(fixed)
            if "private_key" in creds_data:
                creds_data["private_key"] = creds_data["private_key"].replace("\\n", "\n")
            client = gspread.service_account_from_dict(creds_data)
            sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
            all_rows = sheet.get_all_values()
            total = len(all_rows) - 1  # ヘッダー除く
            if total > 0:
                latest = all_rows[-1]
                date = latest[1] if len(latest) > 1 else ""
                name = latest[2] if len(latest) > 2 else ""
                menu = latest[3] if len(latest) > 3 else ""
                reply_message(reply_token, [
                    {
                        "type": "text",
                        "text": (
                            f"📊 レポート\n\n"
                            f"総セッション数：{total}件\n"
                            f"最新記録：{date}\n"
                            f"生徒：{name}\n"
                            f"内容：{menu}\n\n"
                            f"📎 詳細はスプレッドシートを確認してください。"
                        )
                    }
                ])
            else:
                reply_message(reply_token, [
                    {"type": "text", "text": "まだ記録がありません。"}
                ])
        except Exception as e:
            print(f"[レポートエラー] {e}", flush=True)
            reply_message(reply_token, [
                {"type": "text", "text": "⚠️ データ取得中にエラーが発生しました。"}
            ])

    elif len(cmd) > 5:
        # 5文字以上のテキストはセッション記録として解析
        parse_and_confirm(user_id, reply_token, text)

# ---------- ポストバック処理 ----------
def handle_postback(user_id, reply_token, data):

    # 記録する
    if data == "action=記録":
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [
                {"type": "text", "text": "⚠️ セッションデータが見つかりません。もう一度音声を送ってください。"}
            ])
            return

        # Google Sheetsに書き込み
        try:
            write_to_sheets(session)
        except Exception as e:
            import traceback
            print(f"Sheets error: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

        # 生徒送信確認
        reply_message(reply_token, [
            {
                "type": "text",
                "text": "✅ 記録しました！\n\n生徒に送信しますか？",
                "quickReply": {
                    "items": [
                        {
                            "type": "action",
                            "action": {
                                "type": "postback",
                                "label": "🚀 送信する",
                                "data": "action=送信"
                            }
                        },
                        {
                            "type": "action",
                            "action": {
                                "type": "postback",
                                "label": "📝 スキップ",
                                "data": "action=スキップ"
                            }
                        }
                    ]
                }
            }
        ])

    # 生徒に送信する
    elif data == "action=送信":
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [
                {"type": "text", "text": "⚠️ セッションデータが見つかりません。"}
            ])
            return

        student_name = session.get("studentName", "")
        student_line_id = get_student_line_id(student_name)

        if student_line_id:
            push_message(student_line_id, [
                {
                    "type": "text",
                    "text": (
                        f"【稽古記録】\n"
                        f"メニュー：{session.get('menu', '')}\n"
                        f"メモ：{session.get('memo', '')}\n"
                        f"次回：{session.get('next', '')}"
                    )
                }
            ])
            reply_message(reply_token, [
                {"type": "text", "text": f"✅ {student_name}さんに送信しました！"}
            ])
            # セッションクリア
            sessions.pop(user_id, None)
        else:
            reply_message(reply_token, [
                {"type": "text", "text": f"⚠️ 「{student_name}」のLINE IDが登録されていません。\nstudents.jsonに追加してください。"}
            ])

    # スキップ
    elif data == "action=スキップ":
        reply_message(reply_token, [
            {"type": "text", "text": "✅ スキップしました。"}
        ])
        sessions.pop(user_id, None)

    # やり直す
    elif data == "action=retry":
        sessions.pop(user_id, None)
        reply_message(reply_token, [
            {"type": "text", "text": "もう一度音声を送ってください 🎤"}
        ])

# ---------- Google Sheets書き込み ----------
def write_to_sheets(session):
    import re
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS not set")

    try:
        creds_data = json.loads(creds_json)
    except json.JSONDecodeError:
        # private_keyに実改行が混入している場合を修正
        fixed = re.sub(
            r'("private_key"\s*:\s*")(.*?)(")',
            lambda m: m.group(1) + m.group(2).replace('\n', '\\n') + m.group(3),
            creds_json,
            flags=re.DOTALL
        )
        creds_data = json.loads(fixed)

    if "private_key" in creds_data:
        creds_data["private_key"] = creds_data["private_key"].replace("\\n", "\n")

    print(f"[SHEETS] client_email: {creds_data.get('client_email')}", flush=True)
    print(f"[SHEETS] SHEET_ID: {SHEET_ID}", flush=True)
    client = gspread.service_account_from_dict(creds_data)

    sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
    all_rows = sheet.get_all_values()
    no = len(all_rows) - 1  # ヘッダー行を除いた行数

    sheet.append_row([
        no,
        datetime.now().strftime("%Y-%m-%d"),
        session.get("studentName", ""),
        session.get("menu", ""),
        session.get("memo", ""),
        "",  # トレーナー所見（空欄）
        session.get("next", ""),
        "未送信"
    ])

# ---------- 生徒のLINE ID取得 ----------
def get_student_line_id(student_name):
    try:
        with open("data/students.json", "r", encoding="utf-8") as f:
            students = json.load(f)
        return students.get(student_name)
    except Exception as e:
        print(f"students.json error: {e}")
        return None

# ---------- 起動 ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
