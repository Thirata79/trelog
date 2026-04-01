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
            print(f"[ERROR] {e}", flush
