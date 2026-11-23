# =====================================================================
# test_model_preferences.py —— 模型偏好的「谁在生效」
#
# 背景（2026-11-22 界面报「切换不了厂商」）：
#   set_model_preference 每次切换都插一条新行，只把旧行的 is_default 翻 False。
#   前端「取当前偏好」历史上读的是同 task_type 下的**第一条**，
#   于是界面永远显示第一次选的厂商——点了「设为默认」纹丝不动，
#   而后端其实已经换人（界面与实际生效的模型对不上）。
#   这里锁住两条：默认行必须排第一；同一厂商反复切换不堆行。
# =====================================================================

from __future__ import annotations


def _two_vendors(store):
    a = store.create_api_provider(kind="llm", name="deepseek", label="DeepSeek",
                                  api_key="ds", config={"model": "deepseek-flash"})
    b = store.create_api_provider(kind="llm", name="zhipu", label="智谱 GLM",
                                  api_key="zk", config={"model": "GLM-5.3-Flash"})
    return a, b


def test_list_preference_puts_default_row_first(store):
    """默认行排在同类第一位：前端取「第一条」才拿得到当前默认。"""
    a, b = _two_vendors(store)
    store.set_model_preference(task_type="chat", provider_id=a.id, model_name="deepseek-flash")
    store.set_model_preference(task_type="chat", provider_id=b.id, model_name="GLM-5.3-Flash")

    rows = store.list_model_preferences()
    chat_rows = [r for r in rows if r.task_type == "chat"]
    assert chat_rows[0].is_default is True
    assert chat_rows[0].provider_id == b.id


def test_switching_vendor_updates_row_instead_of_stacking(store):
    """切回切去不堆行：同一 (task_type, provider_id) 复用那一行。"""
    a, b = _two_vendors(store)
    first = store.set_model_preference(task_type="chat", provider_id=a.id, model_name="deepseek-flash")
    store.set_model_preference(task_type="chat", provider_id=b.id, model_name="GLM-5.3-Flash")
    again = store.set_model_preference(task_type="chat", provider_id=a.id, model_name="deepseek-flash")

    assert again.id == first.id
    chat_rows = [r for r in store.list_model_preferences() if r.task_type == "chat"]
    assert len(chat_rows) == 2
    assert again.is_default is True


def test_get_model_preference_follows_the_switch(store, settings):
    """后端真正取默认的那一行，必须跟着界面切换走。"""
    from app.llm import resolve_chat

    a, b = _two_vendors(store)
    store.set_model_preference(task_type="chat", provider_id=a.id, model_name="deepseek-flash")
    store.set_model_preference(task_type="chat", provider_id=b.id, model_name="GLM-5.3-Flash")

    pref = store.get_model_preference("chat")
    assert pref is not None and pref.provider_id == b.id
    cfg = resolve_chat(settings, store)
    assert cfg is not None and cfg.provider == "zhipu"
