"""Универсальный retry-обвес поверх ``httpx.Client`` (ADR-0008 §4).

Учебный паттерн прод-HTTP-клиента в распределённом сервисе:

* connect-retry — пересоздание TCP-коннекта, если он отвалился на
  первом байте (rollout соседнего пода, network blip);
* 5xx-retry — если сервер ответил ``500/502/503/504``, повторяем
  запрос; 4xx НЕ ретраим — это уже бизнес-ошибка вызывающего;
* экспоненциальный backoff с jitter — ``base * 2**n + uniform(0, base)``,
  чтобы при массовом одновременном падении сервиса все клиенты не
  заваливали восстановившийся под одной и той же миллисекундой.

Сам ``httpx.HTTPTransport(retries=N)`` ретраит только connect-ошибки и
не покрывает 5xx-сценарии, поэтому держим тонкую обёртку здесь.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 5xx, которые осмысленно ретраить. 501 (Not Implemented) и 505
# (HTTP version not supported) выбиваются — это контрактная проблема,
# не транзиентная.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

# httpx-исключения, означающие транзиентную сетевую неудачу.
_RETRYABLE_HTTPX_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True)
class RetryConfig:
    """Параметры повторов для одного логического вызова."""

    max_attempts: int = 3
    base_delay_sec: float = 0.1
    max_delay_sec: float = 2.0


def request_with_retry(
    call: Callable[[], httpx.Response],
    *,
    config: RetryConfig | None = None,
    operation: str = "http",
) -> httpx.Response:
    """Выполнить ``call`` с экспоненциальным backoff и jitter.

    ``call`` — нулевой callable, который при вызове возвращает
    ``httpx.Response`` или бросает ``httpx.HTTPError``. Изоляция в виде
    callable нужна потому, что ``httpx.Client.post(...)`` нельзя
    «сохранить и повторить» — приходится передавать готовое замыкание.
    """
    cfg = config or RetryConfig()
    last_exc: BaseException | None = None

    for attempt in range(1, cfg.max_attempts + 1):
        try:
            response = call()
        except _RETRYABLE_HTTPX_EXC as exc:
            last_exc = exc
            if attempt == cfg.max_attempts:
                logger.warning(
                    "%s: giving up after %d attempts, last error %s: %s",
                    operation,
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                raise
            _sleep_backoff(
                attempt, cfg, exc_name=type(exc).__name__, operation=operation
            )
            continue

        if response.status_code in _RETRYABLE_STATUS and attempt < cfg.max_attempts:
            _sleep_backoff(
                attempt,
                cfg,
                exc_name=f"HTTP {response.status_code}",
                operation=operation,
            )
            continue

        return response

    # Сюда мы попадаем только если последний attempt был сетевым
    # исключением И мы зачем-то не подняли его. Не должно случаться.
    assert last_exc is not None
    raise last_exc


def _sleep_backoff(
    attempt: int,
    cfg: RetryConfig,
    *,
    exc_name: str,
    operation: str,
) -> None:
    delay = min(cfg.base_delay_sec * (2 ** (attempt - 1)), cfg.max_delay_sec)
    delay += random.uniform(0.0, cfg.base_delay_sec)
    logger.info(
        "%s: retry %d/%d after %.3fs (cause: %s)",
        operation,
        attempt,
        cfg.max_attempts,
        delay,
        exc_name,
    )
    time.sleep(delay)


__all__ = ["RetryConfig", "request_with_retry"]
