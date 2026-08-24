"""Conversation 层。

attempt 持有 conversation，adapter 持有 session：本包只管多轮输入的结构、
校验、legacy 映射与 attempt 级 deadline，不碰 wire canonical 事实。
"""
