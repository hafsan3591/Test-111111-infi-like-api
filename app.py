from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import os
import like_pb2
import like_count_pb2
import uid_generator_pb2
import threading
import urllib3
from datetime import datetime, timezone

# -------------------------------
# API KEY CONFIGURATION
# -------------------------------
API_KEY = "hkysxbhyws"
WRONG_KEY_RESPONSE = {
    "error": "Unauthorized",
    "credits": "@HAFSAN_KUN"
}

TOKENS_PER_FILE = 111
LIKES_PER_TOKEN_PER_DAY = 150
LIKES_PER_USER_REQUEST = 100
TOKEN_FILES = ["token_bd_1.json", "token_bd_2.json"]
GUEST_ID_FILE = "token_info.json"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATE_FILE = "/tmp/token_usage_state.json"
state_lock = threading.Lock()


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    default = {
        "date": _today_utc(),
        "usage": {},
        "file_pointer": 0,
        "file_used": {},
        "guest_id_pointer": 0,
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("date") != _today_utc():
            print("New UTC day -> resetting limits")
            state["date"] = _today_utc()
            state["usage"] = {}
            state["file_pointer"] = 0
            state["file_used"] = {}
            # keep guest_id_pointer across days
            state.setdefault("guest_id_pointer", 0)
        state.setdefault("usage", {})
        state.setdefault("file_pointer", 0)
        state.setdefault("file_used", {})
        state.setdefault("guest_id_pointer", 0)
        return state
    except Exception as e:
        print(f"State load error: {e}")
        return default


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State save error: {e}")


def load_token_file(file_name):
    try:
        with open(file_name, "r") as f:
            tokens = json.load(f)
        if isinstance(tokens, list) and all(isinstance(t, dict) and "token" in t for t in tokens):
            return tokens
        print(f"Warning: {file_name} invalid format.")
        return []
    except FileNotFoundError:
        print(f"Warning: {file_name} not found.")
        return []
    except json.JSONDecodeError:
        print(f"Warning: {file_name} invalid JSON.")
        return []


def load_all_tokens():
    all_tokens = []
    for fname in TOKEN_FILES:
        tokens = load_token_file(fname)
        print(f"Loaded {len(tokens)} tokens from {fname}")
        for t in tokens:
            all_tokens.append((fname, t))
    return all_tokens


# -------------------------------
# GUEST ID MANAGEMENT (for profile view)
# -------------------------------
def load_guest_accounts():
    """
    Load guest accounts from GUEST_ID_FILE.
    Expected formats supported:
      1) [{"uid": "123", "token": "abc"}, ...]
      2) [{"uid": 123, "token": "abc"}, ...]
      3) ["123", "456", ...]  → uid only (token will be picked from token files)
      4) [{"id": "123", "token": "abc"}, ...]
      5) {"accounts": [{"uid": "...", "token": "..."}, ...]}
    Returns list of dicts: [{"uid": "123", "token": "abc"} or {"uid": "123"}]
    """
    try:
        with open(GUEST_ID_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Warning: {GUEST_ID_FILE} not found.")
        return []
    except json.JSONDecodeError:
        print(f"Warning: {GUEST_ID_FILE} invalid JSON.")
        return []

    raw_list = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("accounts", "ids", "uids", "guest_ids", "data"):
            if key in data and isinstance(data[key], list):
                raw_list = data[key]
                break

    accounts = []
    seen = set()
    for item in raw_list:
        acc = {}
        if isinstance(item, dict):
            uid = None
            for k in ("uid", "id", "UID", "ID", "user_id", "krishna_"):
                if k in item and item[k] is not None:
                    uid = str(item[k])
                    break
            if uid and uid.isdigit():
                acc["uid"] = uid
                tok = item.get("token") or item.get("access_token") or item.get("auth")
                if isinstance(tok, str) and tok.strip():
                    acc["token"] = tok.strip()
        elif isinstance(item, (str, int)):
            uid = str(item)
            if uid.isdigit():
                acc["uid"] = uid
        if "uid" in acc and acc["uid"] not in seen:
            seen.add(acc["uid"])
            accounts.append(acc)

    print(f"Loaded {len(accounts)} guest accounts from {GUEST_ID_FILE}")
    return accounts


def get_next_guest_account(guest_accounts, state):
    """Round-robin guest account selection. Returns dict or None."""
    if not guest_accounts:
        return None
    ptr = state.get("guest_id_pointer", 0) % len(guest_accounts)
    acc = guest_accounts[ptr]
    state["guest_id_pointer"] = (ptr + 1) % len(guest_accounts)
    return acc


# -------------------------------
# ENCRYPTION & PROTOBUF HELPERS
# -------------------------------
def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded)).decode('utf-8')


def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()


def create_protobuf_for_profile_check(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return message.SerializeToString()


def enc_profile_check_payload(uid):
    return encrypt_message(create_protobuf_for_profile_check(uid))


async def send_single_like_request(encrypted_like_payload, token_dict, url):
    edata = bytes.fromhex(encrypted_like_payload)
    token_value = token_dict.get("token", "")
    if not token_value:
        return 999
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status
    except asyncio.TimeoutError:
        return 998
    except Exception:
        return 997


def make_profile_check_request(encrypted_profile_payload, server_name, bearer_token):
    """
    Fetch profile info using guest account's bearer token.
    """
    if not bearer_token:
        return None
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ppmainecoonghj.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_profile_payload)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {bearer_token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        return decode_protobuf_profile_info(response.content)
    except Exception as e:
        print(f"Profile check error: {e}")
        return None


def decode_protobuf_profile_info(binary_data):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary_data)
        return items
    except Exception as e:
        print(f"Decode error: {e}")
        return None


def get_like_api_url(server_name):
    if server_name == "IND":
        return "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/LikeProfile"
    else:
        return "https://clientbp.ppmainecoonghj.com/LikeProfile"


def select_tokens_for_likes(all_tokens, required_count, state):
    usage = state["usage"]
    selected = []
    pointer = state.get("file_pointer", 0)

    file_order = list(range(len(TOKEN_FILES)))
    file_order = file_order[pointer:] + file_order[:pointer]

    for file_idx in file_order:
        if len(selected) >= required_count:
            break
        fname = TOKEN_FILES[file_idx]
        tokens_in_file = [t for (f, t) in all_tokens if f == fname]
        for token_dict in tokens_in_file:
            if len(selected) >= required_count:
                break
            tok = token_dict["token"]
            used = usage.get(tok, 0)
            if used < LIKES_PER_TOKEN_PER_DAY:
                selected.append((fname, token_dict, used))
        state["file_pointer"] = (file_idx + 1) % len(TOKEN_FILES)

    return selected


def _resolve_guest_token(guest_acc, all_tokens):
    """
    Determine bearer token for a guest account:
      - if guest_acc has its own token → use it
      - else find a token in token files by matching uid, else fallback to any token
    """
    if not guest_acc:
        return None
    if guest_acc.get("token"):
        return guest_acc["token"]

    gid = guest_acc.get("uid")
    if gid:
        for _fname, td in all_tokens:
            tok_uid = str(td.get("uid", "")).strip()
            if tok_uid and tok_uid == gid:
                return td.get("token")
    # last resort: any available token (still allows profile check)
    if all_tokens:
        return all_tokens[0][1].get("token")
    return None


app = Flask(__name__)


@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "api": "Free Fire Like API",
        "status": "online",
        "endpoints": {
            "/like": "?key=YOUR_KEY&uid=UID&server_name=BD",
            "/token_info": "?key=YOUR_KEY"
        },
        "credits": "@HAFSAN_KUN"
    })


@app.route('/like', methods=['GET'])
def handle_requests():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    uid_param = request.args.get("uid")
    server_name_param = request.args.get("server_name", "BD").upper()

    if not uid_param or not str(uid_param).isdigit():
        return jsonify({"error": "Valid UID is required", "credits": "@HAFSAN_KUN"}), 400

    all_tokens = load_all_tokens()
    if not all_tokens:
        return jsonify({
            "error": "No tokens loaded from token_bd_1.json or token_bd_2.json",
            "credits": "@HAFSAN_KUN"
        }), 500

    guest_accounts = load_guest_accounts()
    if not guest_accounts:
        return jsonify({
            "error": f"No guest IDs loaded from {GUEST_ID_FILE}",
            "credits": "@HAFSAN_KUN"
        }), 500

    # ---- Profile check BEFORE (using guest account token) ----
    with state_lock:
        state = load_state()
        guest_acc = get_next_guest_account(guest_accounts, state)
        save_state(state)

    bearer_token = _resolve_guest_token(guest_acc, all_tokens)
    if not bearer_token:
        return jsonify({
            "error": "No bearer token available for profile check",
            "credits": "@HAFSAN_KUN"
        }), 500

    enc_uid = enc_profile_check_payload(uid_param)
    before_info = make_profile_check_request(enc_uid, server_name_param, bearer_token)
    before_likes = int(before_info.AccountInfo.Likes) if (before_info and hasattr(before_info, 'AccountInfo')) else 0
    print(f"UID {uid_param}: before = {before_likes} (guest={guest_acc.get('uid')})")

    # ---- Likes ----
    with state_lock:
        state = load_state()
        selected = select_tokens_for_likes(all_tokens, LIKES_PER_USER_REQUEST, state)
        save_state(state)

    if not selected:
        return jsonify({
            "LikesGivenByAPI": 0,
            "LikesafterCommand": before_likes,
            "LikesbeforeCommand": before_likes,
            "PlayerNickname": "N/A",
            "UID": int(uid_param),
            "status": 2,
            "message": "All tokens reached daily limit. Try again tomorrow (UTC).",
            "credits": "@HAFSAN_KUN"
        })

    like_proto = create_protobuf_message(uid_param, server_name_param)
    enc_like_payload = encrypt_message(like_proto)
    like_url = get_like_api_url(server_name_param)

    async def send_all():
        tasks = [
            send_single_like_request(enc_like_payload, td, like_url)
            for (_f, td, _u) in selected
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(send_all())
    finally:
        loop.close()

    success_count = 0
    with state_lock:
        state = load_state()
        usage = state["usage"]
        for (_f, td, _u), res in zip(selected, results):
            if isinstance(res, int) and res == 200:
                tok = td["token"]
                usage[tok] = usage.get(tok, 0) + 1
                success_count += 1
        save_state(state)

    # ---- Profile check AFTER (rotate to next guest) ----
    with state_lock:
        state = load_state()
        guest_acc_after = get_next_guest_account(guest_accounts, state)
        save_state(state)

    bearer_token_after = _resolve_guest_token(guest_acc_after, all_tokens)
    after_info = make_profile_check_request(enc_uid, server_name_param, bearer_token_after) if bearer_token_after else None
    after_likes = int(after_info.AccountInfo.Likes) if (after_info and hasattr(after_info, 'AccountInfo')) else before_likes
    nickname = "N/A"
    actual_uid = int(uid_param)
    if after_info and hasattr(after_info, 'AccountInfo'):
        actual_uid = int(after_info.AccountInfo.UID)
        if after_info.AccountInfo.PlayerNickname:
            nickname = str(after_info.AccountInfo.PlayerNickname)

    increment = after_likes - before_likes
    status = 1 if increment > 0 else 2

    with state_lock:
        state = load_state()
        usage = state["usage"]
    total_used = sum(usage.values())
    total_cap = len(all_tokens) * LIKES_PER_TOKEN_PER_DAY

    return jsonify({
        "LikesGivenByAPI": increment,
        "LikesafterCommand": after_likes,
        "LikesbeforeCommand": before_likes,
        "PlayerNickname": nickname,
        "UID": actual_uid,
        "status": status,
        "TokensUsedThisRequest": len(selected),
        "SuccessfulSends": success_count,
        "ProfileGuestUID": guest_acc.get("uid") if guest_acc else None,
        "DailyUsage": {
            "used": total_used,
            "capacity": total_cap,
            "remaining": total_cap - total_used
        },
        "credits": "@HAFSAN_KUN"
    })


@app.route('/token_info', methods=['GET'])
def token_info():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    all_tokens = load_all_tokens()
    guest_accounts = load_guest_accounts()
    with state_lock:
        state = load_state()
        usage = state["usage"]

    file_stats = {}
    for fname in TOKEN_FILES:
        tokens = [t for (f, t) in all_tokens if f == fname]
        used = sum(usage.get(t["token"], 0) for t in tokens)
        cap = len(tokens) * LIKES_PER_TOKEN_PER_DAY
        file_stats[fname] = {
            "token_count": len(tokens),
            "used_today": used,
            "capacity_today": cap,
            "remaining_today": cap - used
        }

    total_used = sum(usage.values())
    total_cap = len(all_tokens) * LIKES_PER_TOKEN_PER_DAY

    return jsonify({
        "date_utc": state.get("date"),
        "files": file_stats,
        "guest_profile": {
            "guest_id_file": GUEST_ID_FILE,
            "guest_count": len(guest_accounts),
            "guest_id_pointer": state.get("guest_id_pointer", 0)
        },
        "total": {
            "tokens": len(all_tokens),
            "used_today": total_used,
            "capacity_today": total_cap,
            "remaining_today": total_cap - total_used
        },
        "config": {
            "LIKES_PER_TOKEN_PER_DAY": LIKES_PER_TOKEN_PER_DAY,
            "LIKES_PER_USER_REQUEST": LIKES_PER_USER_REQUEST
        },
        "credits": "@HAFSAN_KUN"
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)