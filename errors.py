"""共享的领域错误类型，供主服务与接驳存储模块共同使用。"""
from __future__ import annotations


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
