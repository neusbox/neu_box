"""start 借条账本：一次性、按属主隔离、到点作废。

账本本身没有 I/O，这里用假时钟把时间推着走 —— 真等 10 秒不值得。
"""

from neu_box.runtime.container_intents import StartIntentStore

CONTAINER = 'b' * 64


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _store(ttl=10.0):
    clock = _Clock()
    return StartIntentStore(ttl_seconds=ttl, clock=clock), clock


def test_take_consumes_the_intent_once():
    store, _clock = _store()
    store.lend(CONTAINER, 'user', 'sbx_user_1.slice')

    assert store.take(CONTAINER, 'user') == 'sbx_user_1.slice'
    assert store.take(CONTAINER, 'user') is None
    # 消费过的借条还留着（client 要查"到底认领了没有"），但状态是已用。
    assert store.peek(CONTAINER, 'user')['consumed_at'] is not None


def test_intents_are_scoped_by_owner():
    store, _clock = _store()
    store.lend(CONTAINER, 'alice', 'sbx_alice_1.slice')

    assert store.take(CONTAINER, 'bob') is None
    assert store.take(CONTAINER, 'alice') == 'sbx_alice_1.slice'


def test_relending_overwrites_the_previous_intent():
    store, _clock = _store()
    store.lend(CONTAINER, 'user', 'sbx_user_1.slice')
    store.lend(CONTAINER, 'user', 'sbx_user_2.slice')

    assert store.take(CONTAINER, 'user') == 'sbx_user_2.slice'


def test_intents_expire():
    store, clock = _store(ttl=10.0)
    store.lend(CONTAINER, 'user', 'sbx_user_1.slice')

    clock.advance(9.5)
    assert store.take(CONTAINER, 'user') == 'sbx_user_1.slice'

    store.lend(CONTAINER, 'user', 'sbx_user_2.slice')
    clock.advance(10.0)
    assert store.peek(CONTAINER, 'user') is None
    assert store.take(CONTAINER, 'user') is None


def test_a_consumed_intent_expires_too():
    """消费过的借条也带寿命，不能让 client 的确认接口对外一直可读。"""
    store, clock = _store(ttl=10.0)
    store.lend(CONTAINER, 'user', 'sbx_user_1.slice')
    store.take(CONTAINER, 'user')

    clock.advance(10.0)

    assert store.peek(CONTAINER, 'user') is None


def test_clear_drops_everything():
    store, _clock = _store()
    store.lend(CONTAINER, 'user', 'sbx_user_1.slice')

    store.clear()

    assert store.peek(CONTAINER, 'user') is None
