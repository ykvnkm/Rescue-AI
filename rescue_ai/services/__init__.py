"""Standalone HTTP services (ADR-0008 §1).

Каждый подмодуль — отдельное FastAPI-приложение, которое может быть
запущено как самостоятельный процесс / k8s-под:

  - ``nav_engine`` — обёртка над ``rescue_ai.navigation.engine.NavigationEngine``;
  - ``detection``  — обёртка над ``rescue_ai.infrastructure.detectors.*``.

API Rescue-AI обращается к ним через HTTP-адаптеры портов
(см. ``rescue_ai.infrastructure.http_navigation_engine`` и
``http_detector``). Когда переменные ``NAV_ENGINE_URL`` /
``DETECTOR_URL`` пусты — wiring остаётся локальным.
"""
