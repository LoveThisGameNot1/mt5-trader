"""The one thread that is allowed to touch the MetaTrader5 module.

The MetaTrader5 python module is a thin IPC client over the terminal. It is not
thread safe: every call has to come from the thread that ran ``initialize()``.
An MCP server, meanwhile, is a stdio process that does not know or care where
its handler code runs. Getting this wrong produces failures that look random --
an ``order_send`` that works nine times and returns garbage the tenth -- so the
whole module is confined to a single private worker thread here, and the rest of
the process talks to it through a queue.

Consequences that are deliberate:

* Calls are serialised. MT5 is a single desktop terminal; there is no throughput
  to win by parallelising it.
* ``initialize`` and ``shutdown`` run on that same thread, so the terminal
  handle never crosses a thread boundary.
* The module is imported *inside* the worker, not at module import time, which
  keeps the rest of the kernel importable and testable on any machine --
  including CI with no Windows and no terminal.
* A call that returns None is not automatically an error: ``symbols_get`` on a
  filter that matches nothing returns an empty tuple, ``copy_rates_from`` past
  the end of history returns None. So there are two entry points, ``call`` for
  "the answer may legitimately be empty" and ``require`` for "this must exist".
"""

import queue
import threading
import time


class Mt5Error(RuntimeError):
    """A MetaTrader5 call failed. Carries the module's own last_error()."""

    def __init__(self, message, code=None, text=None, function=None):
        super().__init__(message)
        self.code = code
        self.text = text
        self.function = function

    def as_dict(self):
        return {
            "error": str(self),
            "function": self.function,
            "last_error_code": self.code,
            "last_error_text": self.text,
        }


class Mt5Unavailable(Mt5Error):
    """The terminal is not running, not logged in, or refused the connection."""


class _Job:
    __slots__ = ("fn", "args", "kwargs", "done", "result", "error")

    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result = None
        self.error = None


class Mt5Bridge:
    """Serialised access to one MetaTrader 5 terminal.

    ``module`` exists so tests can inject a stand-in with the same surface as
    the real MetaTrader5 module. When it is None the real module is imported on
    the worker thread at first use.
    """

    def __init__(self, settings, module=None, autoconnect=True):
        self.settings = settings
        self._module = module
        self._jobs = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._initialized = False
        self._import_error = None
        self._connect_lock = threading.RLock()
        self._last_attempt = 0.0
        self._autoconnect = autoconnect
        self._call_timeout = 30.0
        self._stats = {"calls": 0, "errors": 0, "reconnects": 0}

    # -- lifecycle ---------------------------------------------------------
    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="mt5", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=30)

    def _worker(self):
        self._ready.set()
        while True:
            job = self._jobs.get()
            if job is None:
                self._shutdown_terminal()
                return
            try:
                job.result = job.fn(*job.args, **job.kwargs)
            except Exception as exc:  # noqa: BLE001 - the model must see it, not crash the thread
                job.error = exc
            finally:
                job.done.set()
                self._jobs.task_done()

    def _dispatch(self, fn, args, kwargs, timeout=None):
        self._ensure_thread()
        job = _Job(fn, args, kwargs)
        self._jobs.put(job)
        if not job.done.wait(timeout or self._call_timeout):
            raise Mt5Unavailable("the MetaTrader5 worker did not answer in time",
                                 function=getattr(fn, "__name__", None))
        if job.error is not None:
            raise job.error
        return job.result

    # -- module access -----------------------------------------------------
    def _get_module(self):
        """Runs on the worker thread only."""
        if self._module is not None:
            return self._module
        if self._import_error is not None:
            raise Mt5Unavailable(
                "the MetaTrader5 python module is not importable: %s. "
                "Install it in the same interpreter that runs this server: "
                "pip install MetaTrader5" % self._import_error)
        try:
            import MetaTrader5 as mt5  # noqa: N813 - the module's own name
        except Exception as exc:  # noqa: BLE001
            self._import_error = exc
            raise Mt5Unavailable(
                "the MetaTrader5 python module is not importable: %s. "
                "Install it in the same interpreter that runs this server: "
                "pip install MetaTrader5" % exc)
        self._module = mt5
        return mt5

    def module_signature(self):
        """(version, author) of the installed module, read on the worker thread."""
        def work():
            mt5 = self._get_module()
            return (getattr(mt5, "__version__", None), getattr(mt5, "__author__", None))
        try:
            return self._dispatch(work, (), {}, timeout=5)
        except Mt5Error:
            return (None, None)

    # -- connect -----------------------------------------------------------
    def connect(self, force=False):
        """Initialize the terminal if it is not already up. Idempotent and
        rate-limited: a closed terminal produces one attempt every few seconds,
        not one per tool call."""
        with self._connect_lock:
            if self._initialized and not force:
                return True
            now = time.monotonic()
            if not force and (now - self._last_attempt) < 3.0:
                return self._initialized
            self._last_attempt = now

            def work():
                mt5 = self._get_module()
                kwargs = {}
                if self.settings.terminal_path:
                    kwargs["path"] = self.settings.terminal_path
                if self.settings.login:
                    kwargs["login"] = int(self.settings.login)
                if self.settings.password:
                    kwargs["password"] = self.settings.password
                if self.settings.server:
                    kwargs["server"] = self.settings.server
                if self.settings.portable:
                    kwargs["portable"] = True
                kwargs["timeout"] = 60000
                mt5.initialize(**kwargs)
                error = mt5.last_error()
                info = mt5.terminal_info()
                if info is None:
                    raise Mt5Unavailable(
                        "initialize() did not bring the terminal up. %s"
                        % _describe(error),
                        code=error[0], text=error[1])
                return info

            info = self._dispatch(work, (), {}, timeout=90)
            if self._initialized or force:
                self._stats["reconnects"] += 1
            self._initialized = True
            return bool(info)

    def shutdown(self):
        if self._thread and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=10)
        self._initialized = False

    def _shutdown_terminal(self):
        if self._module is None:
            try:
                self._get_module()
            except Mt5Error:
                return
        try:
            if self._initialized:
                self._module.shutdown()
        except Exception:  # noqa: BLE001
            pass

    # -- calling -----------------------------------------------------------
    def call(self, function, *args, **kwargs):
        """Raw result of a module function. None is passed through."""
        def work():
            mt5 = self._get_module()
            if not self._initialized:
                self._reconnect_inside(function)
            fn = getattr(mt5, function, None)
            if fn is None:
                raise Mt5Error("MetaTrader5 has no function %r" % function, function=function)
            self._stats["calls"] += 1
            result = fn(*args, **kwargs)
            if result is None:
                self._stats["errors"] += 1
            return result
        return self._dispatch(work, (), {})

    def call_with_error(self, function, *args, **kwargs):
        """(result, last_error) -- when the caller wants the error either way."""
        def work():
            mt5 = self._get_module()
            if not self._initialized:
                self._reconnect_inside(function)
            fn = getattr(mt5, function, None)
            if fn is None:
                raise Mt5Error("MetaTrader5 has no function %r" % function, function=function)
            result = fn(*args, **kwargs)
            return result, tuple(mt5.last_error())
        return self._dispatch(work, (), {})

    def require(self, function, *args, what=None, **kwargs):
        """Like call(), but None becomes an Mt5Error.

        An *empty* answer is not an error. ``positions_get()`` with nothing open
        and ``symbols_get()`` on a filter that matches nothing both return an
        empty tuple, which is a perfectly good answer.
        """
        result, error = self.call_with_error(function, *args, **kwargs)
        if result is None:
            raise Mt5Error(
                "%s() returned nothing%s. %s" % (
                    function,
                    (" for %s" % what) if what else "",
                    _describe(error)),
                code=error[0], text=error[1], function=function)
        return result

    def _reconnect_inside(self, function):
        """Runs on the worker thread. A terminal that was restarted between two
        tool calls must not poison every call after it."""
        mt5 = self._get_module()
        if mt5.terminal_info() is not None:
            self._initialized = True
            return
        kwargs = {"timeout": 60000}
        if self.settings.terminal_path:
            kwargs["path"] = self.settings.terminal_path
        if self.settings.login:
            kwargs["login"] = int(self.settings.login)
        if self.settings.password:
            kwargs["password"] = self.settings.password
        if self.settings.server:
            kwargs["server"] = self.settings.server
        if self.settings.portable:
            kwargs["portable"] = True
        mt5.initialize(**kwargs)
        self._initialized = mt5.terminal_info() is not None
        if self._initialized:
            self._stats["reconnects"] += 1

    # -- status ------------------------------------------------------------
    def status(self):
        """Everything a diagnostic needs, in one call, never raising."""
        out = {
            "module_version": None,
            "module_author": None,
            "connected": False,
            "terminal": None,
            "account": None,
            "last_error": None,
            "stats": dict(self._stats),
        }
        out["module_version"], out["module_author"] = self.module_signature()
        try:
            def work():
                mt5 = self._get_module()
                if not self._initialized:
                    self._reconnect_inside("status")
                terminal = mt5.terminal_info()
                account = mt5.account_info()
                return terminal, account, tuple(mt5.last_error())
            terminal, account, error = self._dispatch(work, (), {}, timeout=20)
        except Mt5Error as exc:
            out["last_error"] = exc.as_dict()
            out["connected"] = False
            return out
        out["terminal"] = _terminal_dict(terminal)
        out["account"] = _account_dict(account)
        out["connected"] = terminal is not None and account is not None
        out["last_error"] = _describe(error)
        if not out["connected"]:
            out["last_error"] = {
                "code": error[0],
                "text": error[1],
                "hint": "initialize() ran but the terminal is not answering. "
                        "Is MetaTrader 5 running and logged in?",
            }
        return out


def _describe(error):
    if not error:
        return "no error reported"
    code, text = (list(error) + [None, None])[:2]
    if code == 1:
        return "no error reported"
    hint = ""
    if code == -8:
        hint = " The Algo Trading button in the terminal is off."
    elif code == -6:
        hint = " The login was rejected."
    elif code == -10004:
        hint = " The terminal is not running, or not logged in."
    elif code == -10003:
        hint = " The terminal is not running."
    return "last_error %s: %s.%s" % (code, text, hint)


def _terminal_dict(info):
    if info is None:
        return None
    from . import records
    return records.as_dict(info, [
        "community_account", "community_connection", "connected", "dlls_allowed",
        "trade_allowed", "tradeapi_disabled", "email_enabled", "ftp_enabled",
        "notifications_enabled", "mqid", "build", "maxbars", "codepage",
        "ping_last", "community_balance", "retransmission", "company",
        "name", "language", "path", "data_path", "commondata_path",
    ])


def _account_dict(info):
    if info is None:
        return None
    from . import records
    return records.as_dict(info, [
        "login", "trade_mode", "leverage", "limit_orders", "margin_so_mode",
        "trade_allowed", "trade_expert", "margin_mode", "currency_digits",
        "fifo_close", "balance", "credit", "profit", "equity", "margin",
        "margin_free", "margin_level", "margin_so_call", "margin_so_so",
        "margin_initial", "margin_maintenance", "assets", "liabilities",
        "commission_blocked", "name", "server", "currency", "company",
    ])
