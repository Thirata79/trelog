import os
import json
import re
import tempfile
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from datetime import datetime

app = Flask(__name__)

# ---------- クライアント設定 ----------
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# ---------- セッション一時保存（メモリ内） ----------
sessions = {}
# 記録待ちの生徒名 { userId: studentName }
recording_for = {}

# ========== LINE送信ヘルパー ==========
def push_message(to, messages):
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    requests.post(LINE_PUSH_URL, headers=headers, json={"to": to, "messages": messages})

def reply_message(reply_token, messages):
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    requests.post(LINE_REPLY_URL, headers=headers, json={"replyToken": reply_token, "messages": messages})

# ========== Google Sheets共通クライアント ==========
def get_sheets_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS not set")
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
    return gspread.service_account_from_dict(creds_data)

# ========== 用語・エクササイズ辞書の読み込み ==========
_vocab_cache = {"terms_ja": None, "terms_en": None, "updated": None}

def translate_exercises_to_ja(english_terms):
    """英語エクササイズ名をGPTで日本語カタカナに一括翻訳"""
    if not english_terms:
        return []
    try:
        chunk = english_terms[:100]  # コスト抑制のため100件まで
        res = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "Translate these English exercise names to Japanese katakana. "
                    "Return ONLY a JSON array of translated names in the same order. "
                    "Example: [\"スプリットスクワット\", \"デッドリフト\"]"
                )},
                {"role": "user", "content": json.dumps(chunk)}
            ],
            response_format={"type": "json_object"}
        )
        data = json.loads(res.choices[0].message.content)
        # JSONオブジェクトの場合はvaluesを取得
        if isinstance(data, dict):
            return list(data.values())[0] if data else []
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[翻訳エラー] {e}", flush=True)
        return []

def get_vocabulary():
    """用語リスト＋エクササイズライブラリからWhisper/GPT用の用語を取得（10分キャッシュ）"""
    now = datetime.now()
    if _vocab_cache["terms_ja"] and _vocab_cache["updated"] and (now - _vocab_cache["updated"]).seconds < 600:
        return _vocab_cache["terms_ja"], _vocab_cache["terms_en"]

    terms_ja = []  # 日本語用語（Whisper prompt用）
    terms_en = []  # 英語用語（GPT解析用）
    try:
        client = get_sheets_client()
        wb = client.open_by_key(SHEET_ID)

        # 用語リスト（日本語専門用語）
        try:
            vocab_sheet = wb.worksheet("用語リスト")
            vocab_rows = vocab_sheet.get_all_values()
            for row in vocab_rows[1:]:
                term = row[1] if len(row) > 1 else ""
                if term:
                    terms_ja.append(term)
        except Exception as e:
            print(f"[用語リスト読込] {e}", flush=True)

        # エクササイズライブラリ（英語エクササイズ名）
        en_exercises = []
        try:
            ex_sheet = wb.worksheet("エクササイズライブラリ")
            ex_rows = ex_sheet.get_all_values()
            for row in ex_rows[2:]:
                for cell in row:
                    if cell and cell.strip():
                        en_exercises.append(cell.strip())
        except Exception as e:
            print(f"[エクササイズライブラリ読込] {e}", flush=True)

        terms_en = en_exercises

        # 英語→日本語カタカナ翻訳
        if en_exercises:
            ja_translations = translate_exercises_to_ja(en_exercises)
            terms_ja.extend(ja_translations)
            print(f"[翻訳] {len(ja_translations)}件 カタカナ変換", flush=True)

        _vocab_cache["terms_ja"] = terms_ja
        _vocab_cache["terms_en"] = terms_en
        _vocab_cache["updated"] = now
        print(f"[用語] 日本語{len(terms_ja)}件 / 英語{len(terms_en)}件", flush=True)
    except Exception as e:
        print(f"[用語読込エラー] {e}", flush=True)

    return terms_ja or [], terms_en or []

# ========== 生徒マスターからLINE ID取得 ==========
def normalize_name(name):
    """名前の表記揺れを吸収（スペース全角半角除去）"""
    return name.replace(" ", "").replace("\u3000", "").strip()

def get_student_line_id(student_name):
    """生徒マスターシートから生徒名でLINE IDを検索
    シート構造: A=ID, B=生徒名, C=保護者名, D=保護者LINE UserID
    スペースの有無・全角半角を無視してマッチング
    """
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).worksheet("生徒マスター")
        rows = sheet.get_all_values()
        target = normalize_name(student_name)
        # rows[0]=タイトル行, rows[1]=ヘッダー行, rows[2:]以降=データ
        for row in rows[2:]:
            name = row[1] if len(row) > 1 else ""       # B列: 生徒名
            line_id = row[3] if len(row) > 3 else ""     # D列: 保護者LINE UserID
            if normalize_name(name) == target and line_id:
                return line_id
        return None
    except Exception as e:
        print(f"[生徒マスターエラー] {e}", flush=True)
        return None

# ========== Webhook ==========
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
            try:
                reply_message(reply_token, [{"type": "text", "text": "処理中にエラーが発生しました。もう一度お試しください。"}])
            except Exception:
                pass
    return jsonify({"status": "ok"})

# ========== 音声処理 ==========
def handle_audio(user_id, reply_token, message_id):
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    res = requests.get(f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        f.write(res.content)
        audio_path = f.name
    # 日本語用語をWhisperのpromptに渡して認識精度UP
    try:
        terms_ja, _ = get_vocabulary()
        whisper_prompt = "空手道場の稽古記録。" + "、".join(terms_ja[:100]) if terms_ja else ""
    except Exception as e:
        print(f"[用語読込スキップ] {e}", flush=True)
        whisper_prompt = ""

    with open(audio_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1", file=audio_file, language="ja",
            prompt=whisper_prompt
        )
    # 事前に生徒が選択されていればその名前を渡す
    selected_student = recording_for.get(user_id, "")
    parse_and_confirm(user_id, reply_token, transcript.text, selected_student)

# ========== GPT解析＋確認（音声・テキスト共通） ==========
def parse_and_confirm(user_id, reply_token, text, selected_student=""):
    # 用語リスト（スペル補正用のみ。GPTの解析内容には影響させない）
    exercise_hint = ""
    try:
        terms_ja, terms_en = get_vocabulary()
        if terms_ja:
            exercise_hint = f"\n\nSpelling reference only (do NOT add exercises not mentioned by the trainer): {', '.join(terms_ja[:80])}"
    except Exception as e:
        print(f"[用語読込スキップ] {e}", flush=True)

    gpt_res = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant that extracts session notes from a Japanese fitness/martial arts trainer. "
                    "Always respond with valid JSON only, no markdown, no explanation. "
                    'Format: {"Student name":"name","Menu":"what was done","Memo":"observations","Next":"next steps"}'
                    "\n\nIMPORTANT RULES:"
                    "\n- Only extract what the trainer ACTUALLY said. Do NOT invent or add content."
                    "\n- Menu: list ONLY exercises explicitly mentioned. Do NOT guess or add extras."
                    "\n- Memo: summarize ONLY what the trainer observed. Do NOT embellish."
                    "\n- Next: state ONLY what the trainer said about next steps. If not mentioned, leave empty."
                    "\n- The spelling reference below is ONLY for correcting misspellings (e.g. 内線→内旋). Do NOT use it to add exercises."
                    "\n- Fix common voice input errors: サンセット→3セット, ゴセット→5セット, 中回→10回, etc. Interpret in fitness context."
                    + exercise_hint
                )
            },
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"}
    )
    data = json.loads(gpt_res.choices[0].message.content)
    # 事前に生徒が選択されていればそちらを優先
    student = selected_student if selected_student else data.get("Student name", "")
    menu = data.get("Menu", "")
    memo = data.get("Memo", "")
    next_session = data.get("Next", "")

    sessions[user_id] = {"studentName": student, "menu": menu, "memo": memo, "next": next_session}
    # 記録待ち状態をクリア
    recording_for.pop(user_id, None)

    reply_message(reply_token, [{
        "type": "text",
        "text": (
            f"以下の内容で解析しました\n\n"
            f"生徒：{student or '（未確認）'}\n"
            f"メニュー：{menu}\n"
            f"メモ：{memo}\n"
            f"次回：{next_session}\n\n"
            "この内容で記録しますか？"
        ),
        "quickReply": {"items": [
            {"type": "action", "action": {"type": "postback", "label": "記録する", "data": "action=記録"}},
            {"type": "action", "action": {"type": "postback", "label": "やり直す", "data": "action=retry"}}
        ]}
    }])

# ========== テキスト処理 ==========
def handle_text(user_id, reply_token, text):
    cmd = text.strip()

    # /記録 → 生徒選択画面
    if cmd in ["/記録", "記録"]:
        handle_record_select(user_id, reply_token)

    # /送信 → 未送信レコード一覧を表示
    elif cmd in ["/送信", "送信"]:
        handle_send_list(user_id, reply_token)

    # /準備 → 生徒選択 → 直近2セッション要約＋サジェスト
    elif cmd in ["/準備", "準備"]:
        handle_prep_select(reply_token)

    # /レポート → 直近サマリー
    elif cmd in ["/レポート", "レポート"]:
        handle_report(reply_token)

    elif len(cmd) > 5:
        # 記録待ちの生徒がいればその生徒名付きで解析
        selected_student = recording_for.get(user_id, "")
        parse_and_confirm(user_id, reply_token, text, selected_student)

# ========== /記録: 生徒選択画面 ==========
def handle_record_select(user_id, reply_token):
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).worksheet("生徒マスター")
        rows = sheet.get_all_values()
        items = []
        for row in rows[2:]:
            name = row[1] if len(row) > 1 else ""
            if name:
                label = name[:20]
                items.append({
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": label,
                        "data": f"action=record_for&student={name}"
                    }
                })
        if items:
            reply_message(reply_token, [{
                "type": "text",
                "text": "誰の記録ですか？",
                "quickReply": {"items": items[:13]}
            }])
        else:
            reply_message(reply_token, [{"type": "text", "text": "生徒マスターにデータがありません。"}])
    except Exception as e:
        print(f"[記録選択エラー] {e}", flush=True)
        reply_message(reply_token, [{"type": "text", "text": "データ取得中にエラーが発生しました。"}])

# ========== /送信: 未送信レコード一覧 ==========
def handle_send_list(user_id, reply_token):
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
        all_rows = sheet.get_all_values()

        # 未送信レコードを探す（ステータス列 = index 7）
        unsent = []
        for i, row in enumerate(all_rows[1:], start=2):  # 行番号（1-indexed、ヘッダー除く）
            status = row[7] if len(row) > 7 else ""
            if status == "未送信":
                name = row[2] if len(row) > 2 else ""
                date = row[1] if len(row) > 1 else ""
                unsent.append({"row": i, "name": name, "date": date})

        if not unsent:
            reply_message(reply_token, [{"type": "text", "text": "未送信のレコードはありません。"}])
            return

        # 最新10件まで表示（quickReplyは最大13個）
        items = []
        for rec in unsent[-10:]:
            label = f"{rec['name']} {rec['date']}"
            if len(label) > 20:
                label = label[:20]
            items.append({
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"action=send_row&row={rec['row']}"
                }
            })

        reply_message(reply_token, [{
            "type": "text",
            "text": f"未送信のレコードが{len(unsent)}件あります。\n送信する生徒を選んでください。",
            "quickReply": {"items": items}
        }])

    except Exception as e:
        print(f"[送信一覧エラー] {e}", flush=True)
        import traceback
        traceback.print_exc()
        reply_message(reply_token, [{"type": "text", "text": "データ取得中にエラーが発生しました。"}])

# ========== /準備: 生徒選択画面 ==========
def handle_prep_select(reply_token):
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).worksheet("生徒マスター")
        rows = sheet.get_all_values()
        items = []
        # rows[0]=タイトル行, rows[1]=ヘッダー行, rows[2:]以降=データ
        for row in rows[2:]:
            name = row[1] if len(row) > 1 else ""
            if name:
                label = name[:20]
                items.append({
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": label,
                        "data": f"action=prep&student={name}"
                    }
                })
        if items:
            reply_message(reply_token, [{
                "type": "text",
                "text": "次回準備をする生徒を選んでください。",
                "quickReply": {"items": items[:13]}
            }])
        else:
            reply_message(reply_token, [{"type": "text", "text": "生徒マスターにデータがありません。"}])
    except Exception as e:
        print(f"[準備選択エラー] {e}", flush=True)
        reply_message(reply_token, [{"type": "text", "text": "データ取得中にエラーが発生しました。"}])

# ========== /準備: 直近2セッション要約＋サジェスト ==========
def handle_next_prep(reply_token, student_name):
    try:
        print(f"[準備] 生徒={student_name}", flush=True)
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
        all_rows = sheet.get_all_values()

        # その生徒の記録を抽出（名前の表記揺れ対応）
        target = normalize_name(student_name)
        print(f"[準備] 検索ターゲット={target} 全行数={len(all_rows)}", flush=True)
        student_rows = []
        for row in all_rows[1:]:
            name = row[2] if len(row) > 2 else ""
            if normalize_name(name) == target:
                student_rows.append(row)

        print(f"[準備] {student_name}の記録数={len(student_rows)}", flush=True)
        if not student_rows:
            reply_message(reply_token, [{"type": "text", "text": f"{student_name}さんの記録がまだありません。"}])
            return

        # 直近2件を取得
        recent = student_rows[-2:]
        session_text = ""
        for row in recent:
            date = row[1] if len(row) > 1 else ""
            menu = row[3] if len(row) > 3 else ""
            memo = row[4] if len(row) > 4 else ""
            trainer = row[5] if len(row) > 5 else ""
            next_note = row[6] if len(row) > 6 else ""
            session_text += f"日付:{date} メニュー:{menu} メモ:{memo} 所見:{trainer} 申し送り:{next_note}\n"

        # 生徒マスターの情報も取得
        master_sheet = client.open_by_key(SHEET_ID).worksheet("生徒マスター")
        master_rows = master_sheet.get_all_values()
        student_info = ""
        for row in master_rows[2:]:
            name = row[1] if len(row) > 1 else ""
            if normalize_name(name) == target:
                age = row[5] if len(row) > 5 else ""
                goal = row[6] if len(row) > 6 else ""
                caution = row[7] if len(row) > 7 else ""
                level = row[8] if len(row) > 8 else ""
                student_info = f"年齢:{age} 目標:{goal} 注意事項:{caution} レベル:{level}"
                break

        # GPTで要約＋サジェスト
        prompt = (
            f"以下は空手道場の生徒「{student_name}」の情報と直近セッション記録です。\n\n"
            f"【生徒情報】{student_info}\n\n"
            f"【直近セッション】\n{session_text}\n"
            f"上記を踏まえて、以下を簡潔に日本語で答えてください：\n"
            f"1. 直近2回の稽古の要約（3行以内）\n"
            f"2. 成長ポイント\n"
            f"3. 次回セッションで取り組むべきこと（具体的な提案）"
        )

        gpt_res = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "あなたは空手道場のトレーナーアシスタントです。簡潔で実用的なアドバイスをしてください。"},
                {"role": "user", "content": prompt}
            ]
        )
        summary = gpt_res.choices[0].message.content

        reply_message(reply_token, [{
            "type": "text",
            "text": f"【{student_name}さん 次回準備】\n\n{summary}"
        }])

    except Exception as e:
        print(f"[準備エラー] {e}", flush=True)
        import traceback
        traceback.print_exc()
        reply_message(reply_token, [{"type": "text", "text": "データ取得中にエラーが発生しました。"}])

# ========== /レポート: 直近サマリー ==========
def handle_report(reply_token):
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
        all_rows = sheet.get_all_values()
        total = len(all_rows) - 1
        if total > 0:
            latest = all_rows[-1]
            date = latest[1] if len(latest) > 1 else ""
            name = latest[2] if len(latest) > 2 else ""
            menu = latest[3] if len(latest) > 3 else ""
            reply_message(reply_token, [{"type": "text", "text": (
                f"レポート\n\n"
                f"総セッション数：{total}件\n"
                f"最新記録：{date}\n"
                f"生徒：{name}\n"
                f"内容：{menu}\n\n"
                f"詳細はスプレッドシートを確認してください。"
            )}])
        else:
            reply_message(reply_token, [{"type": "text", "text": "まだ記録がありません。"}])
    except Exception as e:
        print(f"[レポートエラー] {e}", flush=True)
        reply_message(reply_token, [{"type": "text", "text": "データ取得中にエラーが発生しました。"}])

# ========== ポストバック処理 ==========
def handle_postback(user_id, reply_token, data):
    params = dict(p.split("=", 1) for p in data.split("&") if "=" in p)
    action = params.get("action", "")

    # ---------- 記録対象の生徒を選択 ----------
    if action == "record_for":
        student_name = params.get("student", "")
        recording_for[user_id] = student_name
        reply_message(reply_token, [{"type": "text", "text": f"{student_name}さんですね。\n稽古内容を音声またはテキストで教えてください。"}])
        return

    # ---------- 記録する ----------
    if action == "記録":
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [{"type": "text", "text": "セッションデータが見つかりません。もう一度送ってください。"}])
            return
        try:
            write_to_sheets(session)
        except Exception as e:
            import traceback
            print(f"Sheets error: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

        reply_message(reply_token, [{
            "type": "text",
            "text": "記録しました！\n\n生徒に送信しますか？",
            "quickReply": {"items": [
                {"type": "action", "action": {"type": "postback", "label": "送信する", "data": "action=送信"}},
                {"type": "action", "action": {"type": "postback", "label": "スキップ", "data": "action=スキップ"}}
            ]}
        }])

    # ---------- 記録直後の送信（メモリから） ----------
    elif action == "送信":
        session = sessions.get(user_id)
        if not session:
            reply_message(reply_token, [{"type": "text", "text": "セッションデータが見つかりません。"}])
            return
        student_name = session.get("studentName", "")
        student_line_id = get_student_line_id(student_name)
        if student_line_id:
            push_message(student_line_id, [{"type": "text", "text": (
                f"【稽古記録】\n"
                f"メニュー：{session.get('menu', '')}\n"
                f"メモ：{session.get('memo', '')}\n"
                f"次回：{session.get('next', '')}"
            )}])
            # スプレッドシートのステータスを送信済みに更新
            try:
                update_send_status_by_name(student_name)
            except Exception as e:
                print(f"[ステータス更新エラー] {e}", flush=True)
            reply_message(reply_token, [{"type": "text", "text": f"{student_name}さんに送信しました！"}])
            sessions.pop(user_id, None)
        else:
            reply_message(reply_token, [{"type": "text", "text": f"「{student_name}」のLINE IDが生徒マスターに登録されていません。"}])

    # ---------- 未送信一覧から選択して送信（スプレッドシートから） ----------
    elif action == "send_row":
        row_num = int(params.get("row", 0))
        if row_num < 2:
            reply_message(reply_token, [{"type": "text", "text": "無効なレコードです。"}])
            return
        try:
            client = get_sheets_client()
            sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
            row = sheet.row_values(row_num)
            student_name = row[2] if len(row) > 2 else ""
            menu = row[3] if len(row) > 3 else ""
            memo = row[4] if len(row) > 4 else ""
            next_note = row[6] if len(row) > 6 else ""

            student_line_id = get_student_line_id(student_name)
            if student_line_id:
                push_message(student_line_id, [{"type": "text", "text": (
                    f"【稽古記録】\n"
                    f"メニュー：{menu}\n"
                    f"メモ：{memo}\n"
                    f"次回：{next_note}"
                )}])
                # ステータスを送信済みに更新（H列 = 8番目）
                sheet.update_cell(row_num, 8, "送信済み")
                reply_message(reply_token, [{"type": "text", "text": f"{student_name}さんに送信しました！"}])
            else:
                reply_message(reply_token, [{"type": "text", "text": f"「{student_name}」のLINE IDが生徒マスターに登録されていません。\nスプレッドシートの「生徒マスター」シートにLINE IDを追加してください。"}])
        except Exception as e:
            print(f"[送信エラー] {e}", flush=True)
            import traceback
            traceback.print_exc()
            reply_message(reply_token, [{"type": "text", "text": "送信中にエラーが発生しました。"}])

    # ---------- 次回準備（生徒選択後） ----------
    elif action == "prep":
        student_name = params.get("student", "")
        if student_name:
            handle_next_prep(reply_token, student_name)
        else:
            reply_message(reply_token, [{"type": "text", "text": "生徒名が取得できませんでした。"}])

    # ---------- スキップ ----------
    elif action == "スキップ":
        reply_message(reply_token, [{"type": "text", "text": "スキップしました。"}])
        sessions.pop(user_id, None)

    # ---------- やり直す ----------
    elif action == "retry":
        sessions.pop(user_id, None)
        reply_message(reply_token, [{"type": "text", "text": "もう一度送ってください"}])

# ========== Google Sheets書き込み ==========
def write_to_sheets(session):
    client = get_sheets_client()
    sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
    all_rows = sheet.get_all_values()
    no = len(all_rows) - 1
    sheet.append_row([
        no,
        datetime.now().strftime("%Y-%m-%d"),
        session.get("studentName", ""),
        session.get("menu", ""),
        session.get("memo", ""),
        "",
        session.get("next", ""),
        "未送信"
    ])

# ========== 名前で最新の未送信レコードを送信済みに更新 ==========
def update_send_status_by_name(student_name):
    client = get_sheets_client()
    sheet = client.open_by_key(SHEET_ID).worksheet("セッションログ")
    all_rows = sheet.get_all_values()
    # 下から探して最初に見つかった未送信レコードを更新
    for i in range(len(all_rows) - 1, 0, -1):
        row = all_rows[i]
        name = row[2] if len(row) > 2 else ""
        status = row[7] if len(row) > 7 else ""
        if name == student_name and status == "未送信":
            sheet.update_cell(i + 1, 8, "送信済み")
            break

# ========== 起動 ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
