from app.health import ALERT_AFTER, LONG_PAUSE, SHORT_PAUSE, SoulseekHealth


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_pause_after_errors_grows_with_a_burst():
    clock = Clock()
    health = SoulseekHealth(lambda: True, clock=clock)
    assert health.ready()
    health.failed()
    assert not health.ready()
    clock.now += SHORT_PAUSE
    assert health.ready()
    health.failed()
    health.failed()   # three errors within five minutes: the network, not one uploader
    clock.now += SHORT_PAUSE
    assert not health.ready()
    clock.now += LONG_PAUSE
    assert health.ready()


def test_logged_out_server_blocks_downloads():
    clock, connected = Clock(), [False]
    health = SoulseekHealth(lambda: connected[0], clock=clock)
    assert not health.ready()
    connected[0] = True
    assert not health.ready()        # cached for 30 seconds
    clock.now += 30
    assert health.ready()


def test_one_alert_per_outage_and_one_on_recovery():
    clock, connected, sent = Clock(), [False], []
    health = SoulseekHealth(lambda: connected[0], clock=clock)
    notify = lambda text: sent.append(text) or True
    health.watch(notify, lambda: 7)
    clock.now += ALERT_AFTER - 60
    health.watch(notify, lambda: 7)
    assert sent == []                # short blips stay quiet
    clock.now += 60
    health.watch(notify, lambda: 7)
    clock.now += 60
    health.watch(notify, lambda: 7)
    assert len(sent) == 1 and '10 min' in sent[0] and '7 downloads' in sent[0]
    connected[0] = True
    clock.now += 60
    health.watch(notify, lambda: 7)
    assert len(sent) == 2 and 'reconnected' in sent[1]
    health.watch(notify, lambda: 7)
    assert len(sent) == 2


def test_quiet_recovery_when_no_alert_was_sent():
    clock, connected, sent = Clock(), [False], []
    health = SoulseekHealth(lambda: connected[0], clock=clock)
    health.watch(lambda t: sent.append(t) or True)
    connected[0] = True
    clock.now += 120
    health.watch(lambda t: sent.append(t) or True)
    assert sent == []
