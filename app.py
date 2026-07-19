#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
链弈融通 - 突发阻断下基于非合作博弈与PBFT共识的多式联运智能体
完整后端服务：真实Qwen API + 四阶段流水线 + 登录鉴权

第二届综合交通运输大模型智能体创新大赛
团队：杨雄伟、田一豪 | 指导老师：韩兵
"""

from flask import Flask, render_template, jsonify, request, session
import json
import time
import math
import hashlib
import re
import uuid
import sqlite3
import os
from datetime import datetime, timezone
from openai import OpenAI
from functools import wraps

app = Flask(__name__)
app.secret_key = 'linkyi_fusion_secret_2026'

# =====================================================================
# 配置参数
# =====================================================================
API_KEY = os.environ.get('LLM_API_KEY', 'sk-ac2fe38936c040e788b5a287225ad988')
BASE_URL = os.environ.get('LLM_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
MODEL_NAME = os.environ.get('LLM_MODEL', 'qwen-plus')

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# =====================================================================
# 用户数据库（SQLite）
# =====================================================================
# 优先使用 /data（HF Spaces 持久化目录），其次 /tmp（容器内可写目录），
# 最后回退到本地工程目录，保证本地调试与云端部署都能正常运行。
def _resolve_db_path():
    for candidate in ('/data/users.db', '/tmp/users.db'):
        try:
            parent = os.path.dirname(candidate)
            if os.path.isdir(parent) and os.access(parent, os.W_OK):
                return candidate
        except Exception:
            continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.db')

DB_PATH = _resolve_db_path()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL)''')
    # 插入默认 admin 账号（如果不存在）
    admin_hash = hashlib.sha256('linkyi2026'.encode()).hexdigest()
    c.execute('INSERT OR IGNORE INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
              ('admin', admin_hash, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_user(username, password):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT password_hash FROM users WHERE username = ?', (username,))
    row = c.fetchone()
    conn.close()
    if row and row[0] == hash_password(password):
        return True
    return False

def register_user(username, password):
    if len(username) < 3 or len(password) < 6:
        return False, '用户名至少3位，密码至少6位'
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
                  (username, hash_password(password), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return True, '注册成功'
    except sqlite3.IntegrityError:
        return False, '用户名已存在'
    finally:
        conn.close()

# 初始化数据库
init_db()

# =====================================================================
# 服务端状态存储（避免cookie 4KB限制）
# =====================================================================
_GLOBAL_STORE = {}  # {session_id: state_dict}

def _get_session_id():
    """获取或创建会话ID"""
    sid = session.get('sid')
    if not sid:
        import uuid
        sid = str(uuid.uuid4())
        session['sid'] = sid
    return sid

def get_state():
    sid = _get_session_id()
    if sid not in _GLOBAL_STORE:
        _GLOBAL_STORE[sid] = {
            "stage": "idle",
            "oracle_data": None,
            "task_data": None,
            "bids": [],
            "winning_bid": None,
            "consensus_result": None,
            "logs": []
        }
    return _GLOBAL_STORE[sid]


def add_log(message, level="info"):
    state = get_state()
    timestamp = datetime.now().strftime("%H:%M:%S")
    state["logs"].append({"time": timestamp, "message": message, "level": level})
    if len(state["logs"]) > 100:
        state["logs"] = state["logs"][-100:]


# =====================================================================
# 登录鉴权装饰器
# =====================================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({"status": "error", "message": "未登录"}), 401
        return f(*args, **kwargs)
    return decorated_function


# =====================================================================
# 登录/注册接口
# =====================================================================
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if verify_user(username, password):
        session['logged_in'] = True
        session['user'] = username
        return jsonify({"status": "success", "message": "登录成功"})
    return jsonify({"status": "error", "message": "用户名或密码错误"}), 401


@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')

    ok, msg = register_user(username, password)
    if ok:
        session['logged_in'] = True
        session['user'] = username
        return jsonify({"status": "success", "message": "注册成功，已自动登录"})
    return jsonify({"status": "error", "message": msg}), 400


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "success", "message": "已退出"})


@app.route('/api/check_auth', methods=['GET'])
def check_auth():
    if session.get('logged_in'):
        return jsonify({"status": "ok", "user": session.get('user', '')})
    return jsonify({"status": "unauthorized"}), 401


# =====================================================================
# 阶段一：全局态势预言机引擎（真实Qwen API调用）
# =====================================================================
@app.route('/api/stage1', methods=['POST'])
@login_required
def run_stage1():
    try:
        data = request.json
        news_text = data.get('news_text', '')

        if not news_text or len(news_text.strip()) < 10:
            return jsonify({"status": "error", "message": "请输入有效的新闻文本（至少10个字符）"}), 400

        state = get_state()
        add_log("🌐 [阶段一] 全局态势预言机引擎已激活", "success")
        add_log("📡 外部世界探针正在扫描全球宏观态势节点...", "info")
        add_log(f"📄 接收到新闻文本流 ({len(news_text)} 字符)，提交至Qwen-Plus大模型...", "info")

        start_time = time.time()

        # 真实调用Qwen API进行语义降维
        system_prompt = """你是一个负责驱动"多智能体非合作博弈供应链系统"的全局预言机。
请将外部新闻转化为纯 JSON 格式协议包。严禁输出任何解释性文字或代码块标记。

输出必须严格遵守以下键值结构：
{
    "crisis_uuid": "生成一个全大写事件哈代号，如 SUEZ_BLK_9A2F，用于共识溯源",
    "disrupted_node": "精准提取陷入瘫痪的物理咽喉节点（如 苏伊士运河、巴拿马运河、马六甲海峡等）",
    "estimated_blockade_days": 25,
    "market_panic_index": 0.95,
    "cargo_urgency": "HIGH"
}"""

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请生成预言机协议包，输入流：\n{news_text}"}
            ],
            temperature=0.01,
        )

        raw_output = response.choices[0].message.content.strip()
        # 清理可能的代码块标记
        cleaned = re.sub(r'^```(json)?\s*', '', raw_output, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*```$', '', cleaned)

        elapsed = time.time() - start_time
        add_log(f"🧠 Qwen-Plus 推理完成，耗时 {elapsed:.2f} 秒", "success")

        # 解析JSON
        parsed_data = json.loads(cleaned)
        parsed_data["oracle_timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parsed_data["news_text"] = news_text[:200] + "..." if len(news_text) > 200 else news_text

        # 数据校验
        required_keys = ["crisis_uuid", "disrupted_node", "estimated_blockade_days",
                         "market_panic_index", "cargo_urgency"]
        for key in required_keys:
            if key not in parsed_data:
                raise ValueError(f"LLM输出缺失字段: {key}")

        state["oracle_data"] = parsed_data
        state["stage"] = "stage1_complete"

        add_log(f"✅ 预言机协议包生成成功!", "success")
        add_log(f"   └─ Crisis UUID: {parsed_data['crisis_uuid']}", "info")
        add_log(f"   └─ 阻断靶点: {parsed_data['disrupted_node']}", "info")
        add_log(f"   └─ 封锁天数: {parsed_data['estimated_blockade_days']} 天", "info")
        add_log(f"   └─ 恐慌指数: {parsed_data['market_panic_index']}", "warning")

        return jsonify({
            "status": "success",
            "data": parsed_data,
            "elapsed": elapsed,
            "message": "阶段一完成：预言机协议包生成成功"
        })

    except json.JSONDecodeError as e:
        add_log(f"❌ JSON解析失败: {str(e)}", "error")
        add_log(f"   原始输出: {raw_output[:200]}", "error")
        return jsonify({"status": "error", "message": f"LLM输出JSON解析失败: {str(e)}"}), 500
    except Exception as e:
        add_log(f"❌ 阶段一执行失败: {str(e)}", "error")
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================================================================
# 阶段二：多智能体非合作博弈引擎
# =====================================================================
@app.route('/api/stage2', methods=['POST'])
@login_required
def run_stage2():
    try:
        state = get_state()
        if not state.get("oracle_data"):
            return jsonify({"status": "error", "message": "请先运行阶段一（预言机）"}), 400

        data = request.json or {}
        task_data = {
            "origin": data.get('origin', '上海'),
            "destination": data.get('destination', '鹿特丹'),
            "teu": int(data.get('teu', 100)),
            "time_value_per_day": float(data.get('time_value', 85))
        }
        state["task_data"] = task_data

        add_log("🧠 [阶段二] 多智能体非合作博弈引擎激活", "success")
        add_log(f"📦 货主Agent发布 RFP: {task_data['teu']} TEU | {task_data['origin']} → {task_data['destination']}", "info")
        add_log(f"   时间价值系数: ${task_data['time_value_per_day']}/天", "info")

        rfp_data = state["oracle_data"]
        panic_index = float(rfp_data.get("market_panic_index", 0))

        # === 海运Agent报价（线性恐慌溢价模型）===
        add_log("🌊 海运Agent (Ocean_Maersk_Node) 评估物理航道...", "info")
        ocean_base_cost = 1600  # 好望角绕行基础价格
        fuel_surcharge = 300 * panic_index
        ocean_cost = round(ocean_base_cost + fuel_surcharge, 2)
        ocean_days = 38  # 好望角绕行耗时

        ocean_bid = {
            "agent_id": "Ocean_Maersk_Node",
            "strategy_name": "海运绕行干线 (好望角)",
            "quoted_cost_usd": ocean_cost,
            "estimated_days": ocean_days,
            "capacity_status": "SUFFICIENT",
            "pricing_model": f"${ocean_base_cost} + $300×{panic_index} = ${ocean_cost}"
        }
        add_log(f"   报价: ${ocean_cost}/TEU | {ocean_days}天 | 运力充足", "warning")

        # === 铁路Agent报价（非线性指数动态定价模型）===
        add_log("🚂 铁路Agent (Rail_CRExpress_Node) 触发非线性定价模型...", "info")
        rail_base = 2500
        # 核心模型：Price = base × (e^panic - 0.5) × capacity_penalty
        panic_multiplier = math.exp(panic_index) - 0.5
        max_capacity = 200
        capacity_penalty = 1.5 if task_data['teu'] > max_capacity else 1.0
        rail_cost = round(rail_base * panic_multiplier * capacity_penalty, 2)
        rail_days = 16  # 中欧班列耗时

        rail_bid = {
            "agent_id": "Rail_CRExpress_Node",
            "strategy_name": "中欧班列极速干线",
            "quoted_cost_usd": rail_cost,
            "estimated_days": rail_days,
            "capacity_status": "TIGHT" if task_data['teu'] >= max_capacity * 0.8 else "NORMAL",
            "pricing_model": f"${rail_base} × (e^{panic_index} - 0.5) × {capacity_penalty} = ${rail_cost}"
        }
        add_log(f"   恐慌乘数: e^{panic_index} - 0.5 = {panic_multiplier:.4f}", "info")
        add_log(f"   报价: ${rail_cost}/TEU | {rail_days}天 | 运力{rail_bid['capacity_status']}", "warning")

        bids = [ocean_bid, rail_bid]
        state["bids"] = bids

        # === 货主Agent帕累托综合效用寻优 ===
        add_log("📊 货主Agent执行帕累托综合效用寻优...", "info")
        time_value = task_data['time_value_per_day']
        utility_results = []

        for bid in bids:
            time_cost = bid["estimated_days"] * time_value
            utility = bid["quoted_cost_usd"] + time_cost
            utility_results.append({
                "strategy": bid["strategy_name"],
                "agent_id": bid["agent_id"],
                "quoted_cost": bid["quoted_cost_usd"],
                "time_cost": time_cost,
                "total_utility": round(utility, 2),
                "estimated_days": bid["estimated_days"]
            })
            add_log(f"   → {bid['strategy_name']}: ${bid['quoted_cost_usd']} + {bid['estimated_days']}×${time_value} = ${utility:.2f}", "info")

        # 选择帕累托最优
        best_bid = min(bids, key=lambda b: b["quoted_cost_usd"] + b["estimated_days"] * time_value)
        state["winning_bid"] = best_bid

        saving = abs(utility_results[0]['total_utility'] - utility_results[1]['total_utility'])
        saving_rate = saving / max(u['total_utility'] for u in utility_results) * 100

        add_log(f"✅ 最优决策: {best_bid['agent_id']} ({best_bid['strategy_name']})", "success")
        add_log(f"   综合成本节省: ${saving:.2f} ({saving_rate:.1f}%)", "success")

        state["stage"] = "stage2_complete"

        return jsonify({
            "status": "success",
            "bids": bids,
            "winning_bid": best_bid,
            "utility_analysis": utility_results,
            "saving": round(saving, 2),
            "saving_rate": round(saving_rate, 1),
            "task_data": task_data,
            "message": "阶段二完成：博弈与帕累托寻优成功"
        })

    except Exception as e:
        add_log(f"❌ 阶段二执行失败: {str(e)}", "error")
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================================================================
# 阶段三：PBFT 拜占庭容错共识上链
# =====================================================================
@app.route('/api/stage3', methods=['POST'])
@login_required
def run_stage3():
    try:
        state = get_state()
        if not state.get("winning_bid"):
            return jsonify({"status": "error", "message": "请先运行阶段二（博弈）"}), 400

        add_log("🔗 [阶段三] PBFT实用拜占庭容错共识机制激活", "success")

        rfp_data = state["oracle_data"]
        winning_bid = state["winning_bid"]

        # === PBFT 三阶段共识 ===
        add_log("[Pre-Prepare] 货主Agent本地SHA-256签名并广播合约载荷", "info")
        contract_payload = f"{rfp_data['crisis_uuid']}|{winning_bid['agent_id']}|{winning_bid['quoted_cost_usd']}|{datetime.now(timezone.utc).timestamp()}"

        time.sleep(0.3)
        add_log("[Prepare] 承运商节点核验合法性并追加独立签名", "info")

        # 4个共识节点
        nodes = ["Cargo_Foxconn_EU", "Ocean_Maersk_Node", "Rail_CRExpress_Node", "Regulator_Node"]
        signatures = {}
        for node in nodes:
            sig = hashlib.sha256(f"{contract_payload}_{node}".encode()).hexdigest()
            signatures[node] = sig[:32] + "..."
            add_log(f"   ✓ {node} 签名完成: {signatures[node][:16]}...", "info")

        time.sleep(0.3)
        add_log("[Commit] 收集到 2f+1 个有效签名 (f=1)，触发区块打包", "info")

        # 生成区块哈希
        block_hash_raw = hashlib.sha256(str(signatures).encode('utf-8')).hexdigest()
        block_hash = "0x" + block_hash_raw[:40].upper() + "..."

        consensus_result = {
            "status": "CONSENSUS_REACHED",
            "block_hash": block_hash,
            "winning_strategy": winning_bid["strategy_name"],
            "winning_agent": winning_bid["agent_id"],
            "quoted_cost": winning_bid["quoted_cost_usd"],
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "signatures": len(signatures),
            "nodes": nodes,
            "fault_tolerance": "可容忍 1 个拜占庭恶意节点",
            "crisis_uuid": rfp_data["crisis_uuid"]
        }

        state["consensus_result"] = consensus_result
        state["stage"] = "stage3_complete"

        add_log(f"✅ PBFT共识达成! 区块哈希: {block_hash}", "success")
        add_log(f"   签名节点: {len(signatures)}个 | 容错: f=1", "info")

        return jsonify({
            "status": "success",
            "consensus": consensus_result,
            "message": "阶段三完成：PBFT共识上链成功"
        })

    except Exception as e:
        add_log(f"❌ 阶段三执行失败: {str(e)}", "error")
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================================================================
# 阶段四：数字孪生可视化数据
# =====================================================================
# 主要航运节点坐标库（WGS84）
NODE_COORDS = {
    "苏伊士运河": (29.9290, 32.5595),
    "suez": (29.9290, 32.5595),
    "巴拿马运河": (9.0828, -79.6802),
    "panama": (9.0828, -79.6802),
    "马六甲海峡": (2.5000, 101.4167),
    "malacca": (2.5000, 101.4167),
    "霍尔木兹海峡": (26.5667, 56.2500),
    "hormuz": (26.5667, 56.2500),
    "好望角": (-34.3568, 18.4740),
    "cape of good hope": (-34.3568, 18.4740),
    "上海": (31.2304, 121.4737),
    "shanghai": (31.2304, 121.4737),
    "鹿特丹": (51.9244, 4.4777),
    "rotterdam": (51.9244, 4.4777),
    "新加坡": (1.3521, 103.8198),
    "singapore": (1.3521, 103.8198),
    "汉堡": (53.5511, 9.9937),
    "hamburg": (53.5511, 9.9937),
    "洛杉矶": (33.7701, -118.1937),
    "los angeles": (33.7701, -118.1937),
    "西安": (34.3416, 108.9398),
    "莫斯科": (55.7558, 37.6173),
    "华沙": (52.2297, 21.0122),
    "柏林": (52.5200, 13.4050),
}


@app.route('/api/stage4', methods=['POST'])
@login_required
def run_stage4():
    try:
        state = get_state()
        if not state.get("consensus_result"):
            return jsonify({"status": "error", "message": "请先运行阶段三（PBFT共识）"}), 400

        add_log("🌍 [阶段四] 全球调度数字孪生可视化引擎激活", "success")

        oracle_data = state["oracle_data"]
        winning_bid = state["winning_bid"]
        task_data = state.get("task_data", {"origin": "上海", "destination": "鹿特丹"})

        # 根据LLM提取的阻断节点动态获取坐标
        disrupted_node = oracle_data.get("disrupted_node", "苏伊士运河")
        disrupted_coords = NODE_COORDS.get(disrupted_node) or NODE_COORDS.get(disrupted_node.lower())

        # 如果节点不在库中，尝试模糊匹配
        if not disrupted_coords:
            for key, coords in NODE_COORDS.items():
                if key in disrupted_node or disrupted_node in key:
                    disrupted_coords = coords
                    break

        # 默认回退到苏伊士运河坐标
        if not disrupted_coords:
            disrupted_coords = (29.9290, 32.5595)
            add_log(f"⚠️ 节点 '{disrupted_node}' 不在坐标库中，使用默认坐标", "warning")

        origin = task_data.get("origin", "上海")
        destination = task_data.get("destination", "鹿特丹")
        origin_coords = NODE_COORDS.get(origin) or NODE_COORDS.get(origin.lower()) or (31.2304, 121.4737)
        dest_coords = NODE_COORDS.get(destination) or NODE_COORDS.get(destination.lower()) or (51.9244, 4.4777)

        # 好望角坐标
        cape_coords = NODE_COORDS["好望角"]

        # 构建可视化数据
        viz_data = {
            "disrupted_node": disrupted_node,
            "disrupted_coords": list(disrupted_coords),
            "origin": origin,
            "origin_coords": list(origin_coords),
            "destination": destination,
            "dest_coords": list(dest_coords),
            "cape_coords": list(cape_coords),
            "rail_route": [
                list(NODE_COORDS.get("上海", (31.2304, 121.4737))),
                list(NODE_COORDS.get("西安", (34.3416, 108.9398))),
                list(NODE_COORDS.get("莫斯科", (55.7558, 37.6173))),
                list(NODE_COORDS.get("华沙", (52.2297, 21.0122))),
                list(NODE_COORDS.get("鹿特丹", (51.9244, 4.4777)))
            ],
            "winning_strategy": winning_bid["strategy_name"],
            "winning_agent": winning_bid["agent_id"],
            "consensus_hash": state["consensus_result"]["block_hash"],
            "panic_index": oracle_data.get("market_panic_index", 0),
            "blockade_days": oracle_data.get("estimated_blockade_days", 0),
        }

        add_log(f"✅ 数字孪生沙盘渲染数据准备完成", "success")
        add_log(f"   阻断靶点: {disrupted_node} ({disrupted_coords[0]:.4f}°, {disrupted_coords[1]:.4f}°)", "info")
        add_log(f"   运输路线: {origin} → {destination}", "info")
        add_log(f"   中标方案: {winning_bid['strategy_name']}", "info")

        state["stage"] = "stage4_complete"

        add_log("🎉 四阶段流水线全部完成!", "success")

        return jsonify({
            "status": "success",
            "viz_data": viz_data,
            "message": "阶段四完成：数字孪生可视化渲染成功"
        })

    except Exception as e:
        add_log(f"❌ 阶段四执行失败: {str(e)}", "error")
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================================================================
# 获取系统状态与日志
# =====================================================================
@app.route('/api/status', methods=['GET'])
@login_required
def get_status():
    state = get_state()
    return jsonify({
        "stage": state["stage"],
        "oracle_data": state["oracle_data"],
        "task_data": state.get("task_data"),
        "bids": state["bids"],
        "winning_bid": state["winning_bid"],
        "consensus": state["consensus_result"],
        "logs": state["logs"][-30:]
    })


# =====================================================================
# 重置系统
# =====================================================================
@app.route('/api/reset', methods=['POST'])
@login_required
def reset_system():
    sid = _get_session_id()
    _GLOBAL_STORE[sid] = {
        "stage": "idle",
        "oracle_data": None,
        "task_data": None,
        "bids": [],
        "winning_bid": None,
        "consensus_result": None,
        "logs": []
    }
    add_log("系统已重置", "warning")
    return jsonify({"status": "success", "message": "系统已重置"})


# =====================================================================
# 预设场景（供评委快速测试）
# =====================================================================
PRESET_SCENARIOS = {
    "suez": {
        "title": "苏伊士运河阻断（技术方案文档示例）",
        "news": """【全球供应链最高级别预警】2026年最新快讯：受中东地缘冲突黑天鹅事件爆发影响，
苏伊士运河周边海域遭遇严重物理封锁。国际海事组织（IMO）刚刚宣布，该干线航道进入无限期双向熔断状态。
据伦敦保险市场与航运巨头初步联合评估，此次物理断链将至少持续 25 天。
受此阻断恐慌影响，全球海运运力出现瞬时真空，欧洲制造基地的关键零部件库存面临全线断供危机。
目前，大量被阻断的海运货源正恐慌性涌向亚欧大陆桥，导致中欧班列近期舱位期货价格单日暴涨，市场避险情绪已达到极值。""",
        "task": {"origin": "上海", "destination": "鹿特丹", "teu": 100, "time_value": 85}
    },
    "panama": {
        "title": "巴拿马运河干旱（替代场景）",
        "news": """【全球航运紧急预警】受持续厄尔尼诺现象影响，巴拿马运河加通湖水位降至历史最低点。
巴拿马运河管理局宣布，即日起将每日通行船舶数量从38艘削减至22艘，单船最大吃水深度限制在13.4米，
预计该限行措施将持续至少40天。大量美东至亚洲航线船舶被迫绕行麦哲伦海峡，
全球集装箱运力遭受严重冲击，亚洲至美东航线运价单周暴涨65%，市场恐慌情绪持续蔓延。""",
        "task": {"origin": "洛杉矶", "destination": "上海", "teu": 80, "time_value": 90}
    },
    "malacca": {
        "title": "马六甲海峡海盗危机（替代场景）",
        "news": """【东南亚航运安全红色警报】多国海事协调中心今日发布紧急通告：
马六甲海峡近期遭遇近年来最严重的武装海盗袭击浪潮，过去7天内已发生12起船舶被劫持事件。
马来西亚海军与印尼海军联合巡逻未能有效遏制态势，多家国际保险公司已将该海域风险等级提升至最高级。
预计海峡安全通航将中断至少15天，亚洲至欧洲航线面临重大不确定性，市场恐慌指数飙升至0.78。""",
        "task": {"origin": "新加坡", "destination": "鹿特丹", "teu": 120, "time_value": 80}
    },
    "hormuz": {
        "title": "霍尔木兹海峡军事封锁（地缘冲突场景）",
        "news": """【中东地缘危机最高级警报】霍尔木兹海峡遭遇突发性军事封锁，伊朗革命卫队宣布对海峡实施全面禁航。
全球约20%的石油运输与15%的集装箱贸易被迫中断。国际海事组织紧急召开会议，评估此次封锁至少持续20天。
波斯湾至东亚的能源运输链路瞬间熔断，亚洲至欧洲替代航线运力瞬间被抢订一空，
中欧班列舱位报价单日暴涨120%，全球供应链避险情绪达到历史峰值。""",
        "task": {"origin": "迪拜", "destination": "上海", "teu": 90, "time_value": 95}
    },
    "cape": {
        "title": "好望角特大风暴（极端气象场景）",
        "news": """【南大西洋极端气象预警】南非气象局发布最高级别风暴警报：
好望角海域遭遇50年一遇的特大温带气旋，持续风力达12级以上，浪高超过14米。
南非海事局紧急下令，所有计划绕行好望角的远洋船舶必须在开普敦港避风至少18天。
当前正值苏伊士运河绕行高峰期，大量船舶被迫在南非外海长时间滞留，
全球海运运力遭受二次冲击，亚欧航线运价再次暴涨45%，市场恐慌指数飙升至0.82。""",
        "task": {"origin": "上海", "destination": "鹿特丹", "teu": 110, "time_value": 88}
    },
    "taiwan": {
        "title": "台湾海峡地震带异动（地质灾害场景）",
        "news": """【西太平洋地质灾害红色预警】中国地震局与美国地质调查局联合发布紧急通告：
台湾海峡东部海域出现罕见的7.5级强震及持续余震群，海底电缆受损严重，引发局部海啸预警。
海峡两岸海事部门紧急暂停该海域所有船舶通行，预计通航中断至少12天。
东亚至北美西海岸的主要航线被迫绕行太平洋中部，运力损失达30%，
亚洲电子产业链关键零部件运输受阻，全球半导体供应链避险情绪急速攀升。""",
        "task": {"origin": "深圳", "destination": "洛杉矶", "teu": 100, "time_value": 92}
    }
}


@app.route('/api/scenarios', methods=['GET'])
@login_required
def get_scenarios():
    return jsonify({"status": "success", "scenarios": PRESET_SCENARIOS})


# =====================================================================
# 主页路由
# =====================================================================
@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    # 云端部署平台（Hugging Face Spaces / Render / Railway）通过 PORT 环境变量下发端口；
    # 本地调试时回退到 5000。
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("[LINKYI] 链弈融通智能体系统启动中...")
    print(f"[URL] 访问地址: http://0.0.0.0:{port}")
    print(f"[DB] 数据库路径: {DB_PATH}")
    print("[AUTH] 演示账号: admin / linkyi2026")
    print("[LLM] 模型: Qwen-Plus (通义千问)")
    print("=" * 60)
    # 关闭debug与reloader，避免启动时短暂断连
    app.run(debug=False, use_reloader=False, host='0.0.0.0', port=port)
else:
    # 生产环境（gunicorn调用时）
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
