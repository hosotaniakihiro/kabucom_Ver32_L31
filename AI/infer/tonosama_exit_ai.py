# pj/AI/infer/tonosama_exit_ai.py
def decide_exit_seconds(ai_conf, fast_ret):
    if ai_conf > 0.85 and fast_ret > 0.5:
        return 60
    if ai_conf > 0.75:
        return 45
    return 30
