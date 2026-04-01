import os
import json
import re
import tempfile
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

sessions = {}

def push_message(to, messages):
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    requests.post(LINE_PUSH_URL, headers=headers, json={"to": to, "messages": messages})

def reply_message(reply_token, messages):
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    requests.post(LINE_REPLY_URL, headers=headers, json={"replyToken": reply_token, "messages": messages})

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

def handle_audio(user_id, reply_token, message_id):
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    res = requests.get(f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        f.write(res.content)
        audio_path = f.name
    with open(audio_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file, language="ja")
    text = transcript.text
    gpt_res = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": 'You are a helpful assistant that extracts session notes from a Japanese fitness/martial arts trainer. Always respond with valid JSON only, no markdown, no explanation. Format: {"Student name":"name","Menu":"what was done","Memo":"observations","Next":"next steps"}'},
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"}
    )
    data = json.loads(gpt_res.choices[0].message.content)
    student = data.get("Student name", "")
    menu = data.get("Menu", "")
    memo = data.get("Memo", "")
    next_session = data.get("Next", "")
    sessions[user_id] = {"studentName": student, "menu": menu, "memo": memo, "next": next_session}
    reply_message(reply_token, [{"type": "text", "text": f"✅ 以下の内容で解析しました！\n\n👤 生徒：{student or '（未確認）'}\n📋 メニュー：{menu}\n📝 メモ：{memo}\n🔜 次回：{next_session}\n\nこの内容で記録しますか？", "quickReply": {"items": [{"type": "action", "action": {"type": "postback", "label": "📝 記録する", "data": "action=記録"}}, {"type": "action", "action": {"type": "postback", "label": "✏️ やり直す", "data": "action=retry"}}]}}])

def handle_text(user_id, reply_token, text):
    if text.strip() in ["/記録", "記録"]:
        reply_message(reply_token, [{"type": "text", "text": "話してください 🎤"}])

def handle_postback(user_id, reply_token, data):
    if data == "action=記録":
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [{"type": "text", "text": "⚠️ セッションデータが見つかりません。もう一度音声を送ってください。"}])
            return
        try:
            write_to_sheets(session)
        except Exception as e:
            import traceback
            print(f"Sheets error: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
        reply_message(reply_token, [{"type": "text", "text": "✅ 記録しました！\n\n生徒に送信しますか？", "quickReply": {"items": [{"type": "action", "action": {"type": "postback", "label": "🚀 送信する", "data": "action=送信"}}, {"type": "action", "action": {"type": "postback", "label": "📝 スキップ", "data": "action=スキップ"}}]}}])
    elif data == "action=送信":
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [{"type": "text", "text": "⚠️ セッションデータが見つかりません。"}])
            return
        student_name = session.get("studentName", "")
        student_line_id = get_student_line_id(student_name)
        if student_line_id:
            push_message(student_line_id, [{"type": "text", "text": f"【稽古記録】\nメニュー：{session.get('menu', '')}\nメモ：{session.get('memo', '')}\n次回：{session.get('next', '')}"}])
            reply_message(reply_token, [{"type": "text", "text": f"✅ {student_name}さんに送信しました！"}])
            sessions.pop(user_id, None)
        else:
            reply_message(reply_token, [{"type": "text", "text": f"⚠️ 「{student_name}」のLINE IDが登録されていません。"}])
    elif data == "action=スキップ":
        reply_message(reply_token, [{"type": "text", "text": "✅ スキップしました。"}])
        sessions.pop(user_id, None)
    elif data == "action=retry":
        sessions.pop(user_id, None)
        reply_message(reply_token, [{"type": "text", "text": "もう一度音声を送ってください 🎤"}])

def write_to_sheets(session):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS not set")

    try:
        creds_data = json.loads(creds_json)
    except json.JSONDecodeError:
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
    no = len(all_rows) - 1
    sheet.append_row([no, datetime.now().strftime("%Y-%m-%d"), session.get("studentName", ""), session.get("menu", ""), session.get("memo", ""), "", session.get("next", ""), "未送信"])

def get_student_line_id(student_name):
    try:
        with open("data/students.json", "r", encoding="utf-8") as f:
            students = json.load(f)
        return students.get(student_name)
    except Exception as e:
        print(f"students.json error: {e}")
        return None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
