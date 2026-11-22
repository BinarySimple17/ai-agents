"""Юнит-тесты ValkeyCheckpointer (S08, ревью Minor-9): контракт BaseCheckpointSaver
на фейковом redis-клиенте — без инфры (S00 правило 7; живой Valkey — integration
test_clarify_resume.py).

Самый рискованный новый код S08 — рукописный контракт сейвера: здесь точечно
проверены roundtrip put/get_tuple (channel_values inline), latest-индекс,
first-wins put_writes (+ перезапись спец-каналов WRITES_IDX_MAP), порядок
pending_writes (writes_sort_key), delete_thread, TTL-истёк (ruling R3:
get_tuple → None — раннер синтезирует clarification) и граница ошибок хранилища
(ревью Important-2: redis-сбой → CheckpointStoreError, не сырой RedisError).
"""

import fnmatch
from collections.abc import Iterator
from typing import Any

import pytest
from redis.exceptions import RedisError

from text_to_sql_agent.application.ports.checkpointer import CheckpointStoreError
from text_to_sql_agent.infrastructure.cache import valkey_checkpointer as vc_module
from text_to_sql_agent.infrastructure.cache.valkey_checkpointer import (
    WRITES_IDX_MAP,
    ValkeyCheckpointer,
)

# --- Фейковый redis-клиент: dict-хранилище с TTL по управляемым часам ---


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)


class _FakePipeline:
    def __init__(self, client: "_FakeRedis") -> None:
        self._client = client
        self._ops: list[tuple[str, str, bytes, int | None]] = []

    def set(self, key: str, value: bytes, ex: int | None = None) -> "_FakePipeline":
        self._ops.append(("set", key, value, ex))
        return self

    def execute(self) -> list[bool]:
        return [self._client.set(key, value, ex=ex) for _, key, value, ex in self._ops]


class _FakeRedis:
    """Мини-двойник redis: SETEX-семантика (value, expires_at), SCAN по glob."""

    def __init__(self, clock: _FakeClock, *, fail: bool = False) -> None:
        self._store: dict[str, tuple[bytes, float | None]] = {}
        self.clock = clock
        self.fail = fail  # True — все операции бросают RedisError (сбой хранилища)

    def _norm(self, key: bytes | str) -> str:
        # Контракт redis: SCAN отдаёт bytes — операции с ключами нормализуем
        return key.decode("ascii") if isinstance(key, bytes) else key

    def _expire_ok(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        _, expires_at = entry
        if expires_at is not None and self.clock.t >= expires_at:
            del self._store[key]  # TTL истёк — ключ исчез (ruling R3)
            return False
        return True

    def set(self, key: str, value: bytes | str, ex: int | None = None) -> bool:
        if self.fail:
            raise RedisError("соединение разорвано")
        # Контракт redis: SET хранит байты (str значения кодируются), GET
        # возвращает bytes — адаптер рассчитывает на .decode()
        raw = value.encode("ascii") if isinstance(value, str) else value
        expires_at = None if ex is None else self.clock.t + ex
        self._store[self._norm(key)] = (raw, expires_at)
        return True

    def get(self, key: bytes | str) -> bytes | None:
        if self.fail:
            raise RedisError("соединение разорвано")
        normalized = self._norm(key)
        if not self._expire_ok(normalized):
            return None
        return self._store[normalized][0]

    def exists(self, key: bytes | str) -> int:
        if self.fail:
            raise RedisError("соединение разорвано")
        return 1 if self._expire_ok(self._norm(key)) else 0

    def scan_iter(self, match: str = "") -> Iterator[bytes]:
        if self.fail:
            raise RedisError("соединение разорвано")
        for key in list(self._store):
            if fnmatch.fnmatchcase(key, match) and self._expire_ok(key):
                yield key.encode("ascii")

    def delete(self, *keys: bytes | str) -> int:
        if self.fail:
            raise RedisError("соединение разорвано")
        removed = 0
        for key in keys:  # SCAN отдаёт bytes — нормализуем к str-ключам хранилища
            normalized = key.decode("ascii") if isinstance(key, bytes) else key
            if self._store.pop(normalized, None) is not None:
                removed += 1
        return removed

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def close(self) -> None:  # noqa: B027 — контракт, пустая реализация легальна
        ...


class _FakeRedisFactory:
    """Двойник класса Redis: адаптер вызывает Redis.from_url(url, …)."""

    def __init__(self, client: _FakeRedis) -> None:
        self.client = client

    def from_url(self, url: str, **kwargs: Any) -> _FakeRedis:
        return self.client


def _saver(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, *, fail: bool = False):
    client = _FakeRedis(clock, fail=fail)
    monkeypatch.setattr(vc_module, "Redis", _FakeRedisFactory(client))
    return ValkeyCheckpointer("redis://fake:6379/0", ttl_s=600), client


def _config(thread_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(ckpt_id: str, value: str = "Q1") -> dict[str, Any]:
    return {
        "v": 1,
        "id": ckpt_id,
        "ts": "2026-11-22T00:00:00+00:00",
        "channel_values": {"question": value},
        "channel_versions": {},
        "versions_seen": {},
    }


CKPT1 = "00000001-0000-0000-0000-000000000000"
CKPT2 = "00000002-0000-0000-0000-000000000000"


class TestRoundtrip:
    def test_put_get_tuple_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, _ = _saver(monkeypatch, _FakeClock())
        returned = saver.put(_config("t1"), _checkpoint(CKPT1), {"source": "loop"}, {})
        assert returned["configurable"]["checkpoint_id"] == CKPT1
        tuple_ = saver.get_tuple(_config("t1"))
        assert tuple_ is not None
        assert tuple_.checkpoint["channel_values"]["question"] == "Q1"
        assert tuple_.config["configurable"]["checkpoint_id"] == CKPT1
        assert tuple_.metadata == {"source": "loop"}

    def test_latest_index_returns_newest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # get_tuple БЕЗ checkpoint_id — последний чекпоинт thread (порядок resume)
        saver, _ = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put(_config("t1", CKPT1), _checkpoint(CKPT2, value="Q2"), {}, {})
        tuple_ = saver.get_tuple(_config("t1"))
        assert tuple_ is not None
        assert tuple_.checkpoint["channel_values"]["question"] == "Q2"

    def test_parent_config_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, _ = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put(_config("t1", CKPT1), _checkpoint(CKPT2), {}, {})
        tuple_ = saver.get_tuple(_config("t1", CKPT2))
        assert tuple_ is not None
        assert tuple_.parent_config is not None
        assert tuple_.parent_config["configurable"]["checkpoint_id"] == CKPT1


class TestWrites:
    def test_first_wins_per_task_and_idx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # MemorySaver-семантика: повторная запись того же (task_id, idx) — skip
        saver, _ = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put_writes(_config("t1", CKPT1), [("question", "первая")], "task-a")
        saver.put_writes(_config("t1", CKPT1), [("question", "вторая")], "task-a")
        tuple_ = saver.get_tuple(_config("t1"))
        assert tuple_ is not None
        assert tuple_.pending_writes == [("task-a", "question", "первая")]

    def test_special_channels_always_overwrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # WRITES_IDX_MAP (отрицательные idx — спец-каналы __error__ и др.):
        # перезапись разрешена всегда (семантика MemorySaver)
        special = next(iter(WRITES_IDX_MAP))
        saver, _ = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put_writes(_config("t1", CKPT1), [(special, "раз")], "task-a")
        saver.put_writes(_config("t1", CKPT1), [(special, "два")], "task-a")
        tuple_ = saver.get_tuple(_config("t1"))
        assert tuple_ is not None
        assert tuple_.pending_writes == [("task-a", special, "два")]

    def test_pending_writes_sorted_by_sort_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Порядок pending_writes — writes_sort_key(task_path, task_id, idx)
        saver, _ = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put_writes(_config("t1", CKPT1), [("question", "b")], "task-2", task_path="p2")
        saver.put_writes(_config("t1", CKPT1), [("question", "a")], "task-1", task_path="p1")
        tuple_ = saver.get_tuple(_config("t1"))
        assert tuple_ is not None
        assert [w[0] for w in tuple_.pending_writes] == ["task-1", "task-2"]


class TestLifecycle:
    def test_ttl_expiry_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ruling R3: TTL истёк → ключей нет → get_tuple None (раннер синтезирует
        # clarification-исход, не исключение)
        clock = _FakeClock()
        saver, _ = _saver(monkeypatch, clock)
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        clock.t = 601.0  # TTL 600 истёк
        assert saver.get_tuple(_config("t1")) is None

    def test_delete_thread_clears_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, client = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put_writes(_config("t1", CKPT1), [("question", "w")], "task-a")
        saver.delete_thread("t1")
        assert list(client.scan_iter(match="ckptr:t1:*")) == []
        assert saver.get_tuple(_config("t1")) is None

    def test_list_materializes_desc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver, _ = _saver(monkeypatch, _FakeClock())
        saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        saver.put(_config("t1", CKPT1), _checkpoint(CKPT2), {}, {})
        listed = list(saver.list(_config("t1")))
        assert [t.config["configurable"]["checkpoint_id"] for t in listed] == [CKPT2, CKPT1]


class TestStoreBoundary:
    def test_redis_failure_raises_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ревью S08 Important-2: сырой redis-сбой не выходит из адаптера —
        # типизированный CheckpointStoreError (раннер → валидный fail-исход)
        saver, _ = _saver(monkeypatch, _FakeClock(), fail=True)
        with pytest.raises(CheckpointStoreError, match="хранилище сессий недоступно"):
            saver.get_tuple(_config("t1"))
        with pytest.raises(CheckpointStoreError, match="хранилище сессий недоступно"):
            saver.put(_config("t1"), _checkpoint(CKPT1), {}, {})
        with pytest.raises(CheckpointStoreError, match="хранилище сессий недоступно"):
            saver.delete_thread("t1")

    def test_list_boundary_is_guarded_not_lazy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Re-review M-r4: list был генератором — тело мимо guard-декоратора;
        # материализация гарантирует, что redis-сбой ловится при ВЫЗОВЕ, а не
        # при первом next() (на генераторной версии тест не падал бы)
        saver, _ = _saver(monkeypatch, _FakeClock(), fail=True)
        with pytest.raises(CheckpointStoreError, match="хранилище сессий недоступно"):
            saver.list(_config("t1"))


# --- Allowlist msgpack-сериализации (S27, находка живого resume) ---
# Дефолтный JsonPlusSerializer пропускал незарегистрированные типы с
# deprecation-warning «will be blocked in a future version» — VO из AgentState
# (Assumption на interrupt-чекпоинте) десериализовались «на честном слове».


class _Foreign:
    """Тип вне allowlist — имитация чужого/забытого класса в state."""


def _checkpointer(monkeypatch: pytest.MonkeyPatch) -> ValkeyCheckpointer:
    # Redis.from_url ленив — соединения нет до первой команды; serde-тесты
    # используют только self.serde, инфра не нужна (S00 правило 7)
    return ValkeyCheckpointer("redis://localhost:6399/0", ttl_s=600)


def test_domain_vos_roundtrip_without_unregistered_warning(
    monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
) -> None:
    import warnings as _warnings

    from text_to_sql_agent.domain.entities import Assumption
    from text_to_sql_agent.domain.result_compact import ResultCompact
    from text_to_sql_agent.domain.skeleton import Skeleton

    ckpt = _checkpointer(monkeypatch)
    assumption = Assumption(source="user_answer", text="за 2026 год")
    skeleton = Skeleton(
        tables=("orders", "customers"),
        from_table="orders",
        join_sections=("INNER JOIN customers ON orders.customer_id = customers.customer_id",),
        mandatory_predicates=(),
        skeleton_hash="abc123",
    )
    blob = ckpt.serde.dumps_typed((assumption, skeleton, None))
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")  # «unregistered type» теперь ошибка теста
        assumption_back, skeleton_back, _ = ckpt.serde.loads_typed(blob)
    assert assumption_back == assumption
    assert skeleton_back == skeleton
    assert not [w for w in recwarn if "unregistered" in str(w.message)]
    # ResultCompact — тот же allowlist (pydantic-VO из state, S15)
    compact = ResultCompact(row_count=1, sample=[{"n": 5}])
    blob2 = ckpt.serde.dumps_typed((compact,))
    (compact_back,) = ckpt.serde.loads_typed(blob2)
    assert compact_back == compact


def test_unregistered_type_is_blocked_not_silently_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deny-by-default: тип вне allowlist отвергается громко на DUMP (строже
    # прежнего warning-режима и «будущей блокировки» LangGraph — чекпоинт с
    # забытым VO не запишется молча); новый VO в state добавляется в
    # _CHECKPOINT_MSGPACK_TYPES осознанно
    import pytest as _pytest

    ckpt = _checkpointer(monkeypatch)
    with _pytest.raises(TypeError):
        ckpt.serde.dumps_typed((_Foreign(),))
