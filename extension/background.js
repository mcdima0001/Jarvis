/**
 * Мост между Jarvis и браузером.
 *
 * Расширение подключается к локальному серверу Jarvis и ждёт команд. Именно
 * так, а не наоборот: расширение слушать порт не умеет, только подключаться.
 *
 * Токен лежит в token.json рядом с этим файлом — его пишет туда сам Jarvis.
 * В web_accessible_resources он не объявлен, поэтому страницам сайтов файл
 * недоступен: прочитать его может только само расширение.
 *
 * Служебный поток в Manifest V3 засыпает через полминуты простоя, поэтому
 * тут две страховки: обмен по сокету продлевает жизнь потоку, а будильник
 * поднимает его заново и переподключает, если он всё-таки уснул.
 */

const DEFAULT_PORT = 8765;
const KEEPALIVE_MS = 20000;
const ALARM = "jarvis-reconnect";

let socket = null;
let keepalive = null;

/** Настройки соединения: их пишет Jarvis при запуске. */
async function readConfig() {
  const response = await fetch(chrome.runtime.getURL("token.json"));
  const data = await response.json();
  return { token: data.token || "", port: data.port || DEFAULT_PORT };
}

/** Подключиться, если ещё не подключены. */
async function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  let config;
  try {
    config = await readConfig();
  } catch (error) {
    // Jarvis ещё не запускался и токен не создан — попробуем позже.
    return;
  }

  const url = `ws://127.0.0.1:${config.port}/?token=${encodeURIComponent(config.token)}`;
  socket = new WebSocket(url);

  socket.onopen = () => {
    send({ event: "hello", agent: navigator.userAgent });
    keepalive = setInterval(() => send({ event: "keepalive" }), KEEPALIVE_MS);
  };

  socket.onmessage = (message) => {
    let request;
    try {
      request = JSON.parse(message.data);
    } catch (error) {
      return;
    }
    handle(request);
  };

  socket.onclose = () => {
    clearInterval(keepalive);
    keepalive = null;
    socket = null;
  };

  socket.onerror = () => {
    // Подробности придут в onclose; отдельно реагировать не на что.
  };
}

/** Отправить объект в Jarvis. */
function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

/** Выполнить команду и ответить тем же идентификатором. */
async function handle(request) {
  const { id, action, params } = request;
  if (id === undefined) {
    return;
  }
  try {
    send({ id, ok: true, result: await run(action, params || {}) });
  } catch (error) {
    send({ id, ok: false, error: String((error && error.message) || error) });
  }
}

/** Только обычные веб-адреса: chrome:// и file:// открывать не будем. */
function isWebUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

/**
 * Служебные страницы самого браузера — настройки, расширения, история.
 * Список схем закрытый: file:// и javascript: сюда не попадут ни при каких
 * условиях, даже если такая ссылка придёт по сокету.
 */
function isInternalUrl(url) {
  return typeof url === "string" && /^(browser|chrome|edge|opera):\/\/|^about:/i.test(url);
}

/** Один ли это сайт. www отбрасывается: ya.ru и www.ya.ru — одно и то же. */
function sameSite(first, second) {
  try {
    const left = new URL(first).host.replace(/^www\./, "");
    const right = new URL(second).host.replace(/^www\./, "");
    return left === right;
  } catch (error) {
    return false;
  }
}

/** Показать вкладку и поднять её окно на передний план. */
async function focusTab(tab) {
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return { tabId: tab.id, windowId: tab.windowId, title: tab.title, url: tab.url };
}

/** Выполнить одну команду. */
async function run(action, params) {
  if (action === "ping") {
    return { pong: true };
  }

  if (action === "tabs") {
    const tabs = await chrome.tabs.query({});
    return {
      tabs: tabs.map((tab) => ({
        tabId: tab.id,
        windowId: tab.windowId,
        title: tab.title,
        url: tab.url,
        active: tab.active,
      })),
    };
  }

  if (action === "open" && Array.isArray(params.urls)) {
    // Служебная страница браузера: схема у каждого своя (browser://, chrome://,
    // about:), поэтому Jarvis присылает список, а мы пробуем по очереди.
    let failure = null;
    for (const candidate of params.urls) {
      if (!isInternalUrl(candidate)) {
        continue;
      }
      const existing = (await chrome.tabs.query({})).find((tab) => tab.url === candidate);
      if (existing) {
        return { ...(await focusTab(existing)), reused: true, url: candidate };
      }
      try {
        const tab = await chrome.tabs.create({ url: candidate, active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        return { tabId: tab.id, windowId: tab.windowId, url: candidate, reused: false };
      } catch (error) {
        failure = error;
      }
    }
    throw new Error(failure ? String(failure.message || failure) : "страница недоступна");
  }

  if (action === "open") {
    if (!isWebUrl(params.url)) {
      throw new Error("недопустимый адрес");
    }
    if (params.reuse) {
      const tabs = await chrome.tabs.query({});
      const found = tabs.find((tab) => sameSite(tab.url || "", params.url));
      if (found) {
        return { ...(await focusTab(found)), reused: true };
      }
    }
    const tab = await chrome.tabs.create({ url: params.url, active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
    return { tabId: tab.id, windowId: tab.windowId, reused: false };
  }

  if (action === "activate") {
    const tab = await chrome.tabs.get(params.tabId);
    return await focusTab(tab);
  }

  if (action === "close") {
    const tabs = await chrome.tabs.query({});
    const wanted = params.tabIds || (params.tabId ? [params.tabId] : null);
    const doomed = wanted
      ? tabs.filter((tab) => wanted.includes(tab.id))
      : tabs.filter((tab) => isWebUrl(params.url) && sameSite(tab.url || "", params.url));
    if (!doomed.length) {
      return { closed: 0 };
    }
    await chrome.tabs.remove(doomed.map((tab) => tab.id));
    return { closed: doomed.length, titles: doomed.map((tab) => tab.title) };
  }

  throw new Error(`неизвестная команда: ${action}`);
}

chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) {
    connect();
  }
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
