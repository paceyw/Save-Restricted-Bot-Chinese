import asyncio
import importlib.util
import sys
import types


class _Filter:
    def __and__(self, other):
        return self

    def __invert__(self):
        return self


class _Filters:
    text = _Filter()
    private = _Filter()

    @staticmethod
    def command(*args, **kwargs):
        return _Filter()


class _FakeApp:
    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator


class _Status:
    def __init__(self):
        self.edits = []

    async def edit(self, text):
        self.edits.append(text)


class _Message:
    def __init__(self, text, user_id=7):
        self.text = text
        self.from_user = types.SimpleNamespace(id=user_id)

    async def delete(self):
        return None

    async def reply(self, text):
        return _Status()


class _Storage:
    async def dc_id(self):
        return 4


class _FakeClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.storage = _Storage()
        self.sign_in_code = None
        self.disconnected = False
        _FakeClient.instances.append(self)

    async def connect(self):
        return None

    async def send_code(self, phone):
        return types.SimpleNamespace(
            phone_code_hash="hash",
            type="app",
        )

    async def sign_in(self, phone, phone_code_hash, code):
        self.sign_in_code = code

    async def export_session_string(self):
        return "session-string"

    async def disconnect(self):
        self.disconnected = True

    async def check_password(self, password):
        return None


def _load_login_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = _FakeClient
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.Message = object
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)

    pyrogram_errors = types.ModuleType("pyrogram.errors")
    for name in (
        "BadRequest",
        "SessionPasswordNeeded",
        "PhoneCodeInvalid",
        "PhoneCodeExpired",
        "MessageNotModified",
    ):
        setattr(pyrogram_errors, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)

    config = types.ModuleType("config")
    config.API_ID = 123
    config.API_HASH = "hash"
    monkeypatch.setitem(sys.modules, "config", config)

    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client._WORKDIR = "/tmp"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    func = types.ModuleType("utils.func")

    async def save_user_session(user_id, session):
        return True

    func.save_user_session = save_user_session
    func.get_user_data = lambda user_id: None
    func.remove_user_session = save_user_session
    func.save_user_bot = save_user_session
    func.remove_user_bot = save_user_session
    monkeypatch.setitem(sys.modules, "utils.func", func)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.ecs = lambda value: value
    encrypt.dcs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    fetch = types.ModuleType("plugins.fetch")
    fetch.user_bots = {}
    fetch.user_clients = {}
    fetch.premium_userbot = None
    fetch._locks = {}

    def _client_lock(user_id):
        lock = fetch._locks.get(user_id)
        if lock is None:
            lock = fetch._locks[user_id] = asyncio.Lock()
        return lock

    fetch._client_lock = _client_lock
    monkeypatch.setitem(sys.modules, "plugins.fetch", fetch)
    tasks = types.ModuleType("plugins.tasks")
    tasks._ensure_sweeper = lambda: None
    tasks.register_sweep_hook = lambda _hook: None
    monkeypatch.setitem(sys.modules, "plugins.tasks", tasks)

    custom_filters = types.ModuleType("utils.custom_filters")
    steps = {}

    def set_user_step(user_id, step=None):
        if step is None:
            steps.pop(user_id, None)
        else:
            steps[user_id] = step

    custom_filters.login_in_progress = _Filter()
    custom_filters.set_user_step = set_user_step
    custom_filters.get_user_step = lambda user_id: steps.get(user_id)
    monkeypatch.setitem(sys.modules, "utils.custom_filters", custom_filters)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [
        str(__import__("pathlib").Path(__file__).resolve().parents[1] / "plugins")
    ]
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    sys.modules.pop("plugins.login", None)
    import importlib
    module = importlib.import_module("plugins.login")
    return module, steps


def test_password_step_preserves_leading_and_trailing_whitespace(monkeypatch):
    module, steps = _load_login_module(monkeypatch)
    status = _Status()
    temp_client = _FakeClient("temp", in_memory=True)

    password_calls = []

    async def capture_password(password):
        password_calls.append(password)

    temp_client.check_password = capture_password
    module.login_cache[7] = {
        "status_msg": status,
        "temp_client": temp_client,
    }
    steps[7] = module.STEP_PASSWORD

    asyncio.run(module.handle_login_steps(None, _Message("  my password  ")))

    assert password_calls[0] == "  my password  "


def test_session_password_needed_transitions_to_password_step(monkeypatch):
    module, steps = _load_login_module(monkeypatch)
    status = _Status()
    temp_client = _FakeClient("temp", in_memory=True)

    async def password_needed_sign_in(phone, phone_code_hash, code):
        raise module.SessionPasswordNeeded()

    temp_client.sign_in = password_needed_sign_in
    module.login_cache[7] = {
        "status_msg": status,
        "phone": "+12345678900",
        "phone_code_hash": "hash",
        "temp_client": temp_client,
    }
    steps[7] = module.STEP_CODE

    asyncio.run(module.handle_login_steps(None, _Message("s12345")))

    assert steps[7] == module.STEP_PASSWORD
    assert "🔒 已启用两步验证" in status.edits[-1]
    assert "请输入您的密码" in status.edits[-1]


def test_extract_digits_from_obfuscated_formats(monkeypatch):
    module, _ = _load_login_module(monkeypatch)

    assert module.extract_digits("12345") == "12345"
    assert module.extract_digits("1 2 3 4 5") == "12345"
    assert module.extract_digits("s12345") == "12345"
    assert module.extract_digits("1a2a3a4a5") == "12345"
    assert module.extract_digits("1-2-3-4-5") == "12345"
    assert module.extract_digits("１２３４５") == "12345"
    assert module.extract_digits("") == ""
    assert module.extract_digits(None) == ""

def test_login_handler_normalizes_letter_mixed_code(monkeypatch):
    module, steps = _load_login_module(monkeypatch)
    status = _Status()
    temp_client = _FakeClient("temp", in_memory=True)
    module.login_cache[7] = {
        "status_msg": status,
        "phone": "+12345678900",
        "phone_code_hash": "hash",
        "temp_client": temp_client,
    }
    steps[7] = module.STEP_CODE

    asyncio.run(module.handle_login_steps(None, _Message("s12345")))

    assert temp_client.sign_in_code == "12345"
    assert "✅ 登录成功！！" in status.edits


def test_login_handler_normalizes_spaced_code_before_sign_in(monkeypatch):
    module, steps = _load_login_module(monkeypatch)
    status = _Status()
    temp_client = _FakeClient("temp", in_memory=True)
    module.login_cache[7] = {
        "status_msg": status,
        "phone": "+12345678900",
        "phone_code_hash": "hash",
        "temp_client": temp_client,
    }
    steps[7] = module.STEP_CODE

    asyncio.run(module.handle_login_steps(None, _Message("1 2 3 4 5")))

    assert temp_client.sign_in_code == "12345"
    assert "✅ 登录成功！！" in status.edits


def test_phone_code_expired_requests_fresh_code_without_clearing_flow(
    monkeypatch,
):
    module, steps = _load_login_module(monkeypatch)
    status = _Status()
    temp_client = _FakeClient("temp", in_memory=True)

    async def expired_sign_in(phone, phone_code_hash, code):
        raise module.PhoneCodeExpired()

    temp_client.sign_in = expired_sign_in
    module.login_cache[7] = {
        "status_msg": status,
        "phone": "+12345678900",
        "phone_code_hash": "old-hash",
        "temp_client": temp_client,
        "code_sent_at": 0,
    }
    steps[7] = module.STEP_CODE

    asyncio.run(module.handle_login_steps(None, _Message("12345")))

    assert steps[7] == module.STEP_CODE
    assert "已重新发送验证码" in status.edits[-1]
    assert module.login_cache[7]["phone_code_hash"] == "hash"


def test_login_handler_uses_fresh_in_memory_session_and_compact_code(
    monkeypatch, tmp_path
):
    module, steps = _load_login_module(monkeypatch)
    module._WORKDIR = str(tmp_path)
    stale_session = tmp_path / "temp_7.session"
    stale_session.write_text("stale")

    status = _Status()
    module.login_cache[7] = {
        "status_msg": status,
        "phone": "+12345678900",
        "phone_code_hash": "hash",
    }
    module.login_cache[7]["temp_client"] = _FakeClient(
        "old", in_memory=True
    )
    steps[7] = module.STEP_CODE

    asyncio.run(module.handle_login_steps(None, _Message("12345")))

    client = module.login_cache[7]["status_msg"]
    assert "✅ 登录成功！！" in client.edits
    assert _FakeClient.instances[-1].sign_in_code == "12345"
    assert not stale_session.exists()

    module.login_cache.clear()
    steps.clear()
    status = _Status()
    steps[7] = module.STEP_PHONE
    module.login_cache[7] = {"status_msg": status}
    asyncio.run(module.handle_login_steps(None, _Message("+12345678900")))

    assert _FakeClient.instances[-1].kwargs["in_memory"] is True
