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

// Код, который выполняется внутри страниц, лежит отдельно — в page.js.
// Chrome поднимает служебный поток как worker, и файл подключается вручную;
// Firefox перечисляет оба файла в манифесте, и там importScripts не бывает.
if (typeof importScripts === "function") {
  importScripts("page.js");
}

const DEFAULT_PORT = 8765;
/** Chrome помечает этим вкладку, которая не входит ни в одну группу. */
const NO_GROUP = -1;
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

/**
 * Где пользователь работает прямо сейчас: активная вкладка окна в фокусе.
 * По ней выбирается ближайшая из подходящих — иначе «переключись на гитхаб»
 * уводит в другую группу вкладок, где такой же гитхаб просто оказался первым.
 */
async function currentSpot() {
  try {
    const window = await chrome.windows.getLastFocused({ populate: false });
    const [tab] = await chrome.tabs.query({ active: true, windowId: window.id });
    if (!tab) {
      return null;
    }
    return {
      tabId: tab.id,
      windowId: tab.windowId,
      groupId: tab.groupId === undefined ? NO_GROUP : tab.groupId,
    };
  } catch (error) {
    return null;
  }
}

/** Насколько вкладка близка к текущей: своя группа, своё окно, всё остальное. */
function distance(tab, spot) {
  if (!spot) {
    return 2;
  }
  const sameWindow = tab.windowId === spot.windowId;
  if (sameWindow && spot.groupId !== NO_GROUP && tab.groupId === spot.groupId) {
    return 0;
  }
  return sameWindow ? 1 : 2;
}

/**
 * Отсортировать вкладки от ближних к дальним, а равные по близости — от
 * недавно открытых к забытым.
 */
function byProximity(tabs, spot) {
  return tabs.slice().sort((first, second) => {
    const closer = distance(first, spot) - distance(second, spot);
    return closer || (second.lastAccessed || 0) - (first.lastAccessed || 0);
  });
}

/**
 * В какой вкладке выполнять действие на странице.
 *
 * Порядок важен и отвечает на вопрос «а где, собственно?»:
 *
 * 1. вкладку назвали прямо — номером или сайтом («поставь ютуб на паузу»);
 * 2. вкладка **звучит** — из неё сейчас идёт звук. Это главный признак: «стоп»
 *    почти всегда относится к тому, что играет, а не к тому, куда смотрят;
 * 3. вкладка в фокусе — на неё смотрят прямо сейчас.
 *
 * Названный сайт, которого нет среди открытых, ответа не имеет: жать кнопки
 * в первой попавшейся вкладке вместо него было бы хуже, чем честно отказать.
 */
async function pickTab(params) {
  const chosen = await chooseTab(params);
  return chosen ? await loaded(chosen) : null;
}

async function chooseTab(params) {
  if (params.tabId) {
    try {
      return await chrome.tabs.get(params.tabId);
    } catch (error) {
      // Вкладку успели закрыть — ищем дальше по обычным правилам.
    }
  }

  const spot = await currentSpot();
  const tabs = await chrome.tabs.query({});

  if (isWebUrl(params.url)) {
    return byProximity(tabs.filter((tab) => sameSite(tab.url || "", params.url)), spot)[0] || null;
  }

  // «Нажми первую ссылку» относится к тому, куда смотрят, даже если в соседней
  // вкладке играет музыка. Поэтому звучащую вкладку иногда нужно пропустить.
  if (!params.active) {
    const audible = byProximity(tabs.filter((tab) => tab.audible), spot)[0];
    if (audible) {
      return audible;
    }
  }
  if (spot) {
    return tabs.find((tab) => tab.id === spot.tabId) || null;
  }
  return tabs.find((tab) => tab.active) || null;
}

/**
 * Дождаться, пока страница догрузится.
 *
 * Команда часто идёт сразу за открытием вкладки («включи первое видео» после
 * поиска), а нажимать не на чем: разметки ещё нет. Ждём недолго — если за это
 * время страница не открылась, дело не в спешке.
 */
function loaded(tab, timeout = 5000) {
  if (tab.status === "complete") {
    return Promise.resolve(tab);
  }
  return new Promise((resolve) => {
    const finish = (result) => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve(result);
    };
    const listener = (id, change) => {
      if (id === tab.id && change.status === "complete") {
        chrome.tabs.get(tab.id).then(finish, () => finish(tab));
      }
    };
    const timer = setTimeout(() => finish(tab), timeout);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/**
 * Выполнить в странице то, что умеет page.js.
 *
 * Впрыскивается во все кадры сразу: плеер часто живёт во вложенном фрейме
 * (встроенное видео, виджет плеера), и с одним верхним кадром такие страницы
 * не работают вовсе. Ответ берётся у того кадра, где что-то получилось.
 */
async function inPage(tab, func, args, allFrames) {
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: Boolean(allFrames) },
    func,
    args,
  });
  const values = results.map((item) => item.result).filter(Boolean);
  return values.find((value) => value.done || (value.controls || []).length) || values[0] || {};
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
      current: await currentSpot(),
      tabs: tabs.map((tab) => ({
        tabId: tab.id,
        windowId: tab.windowId,
        groupId: tab.groupId === undefined ? NO_GROUP : tab.groupId,
        title: tab.title,
        url: tab.url,
        active: tab.active,
        // Когда в неё смотрели: из нескольких похожих вкладок почти всегда
        // нужна та, с которой недавно работали.
        lastAccessed: tab.lastAccessed || 0,
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
      const matching = tabs.filter((tab) => sameSite(tab.url || "", params.url));
      const found = byProximity(matching, await currentSpot())[0];
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
      : byProximity(
          tabs.filter((tab) => isWebUrl(params.url) && sameSite(tab.url || "", params.url)),
          await currentSpot(),
        );
    if (!doomed.length) {
      return { closed: 0 };
    }
    await chrome.tabs.remove(doomed.map((tab) => tab.id));
    return { closed: doomed.length, titles: doomed.map((tab) => tab.title) };
  }

  if (action === "target") {
    // Какая вкладка попадёт под команду. Спрашивается заранее: не зная сайта,
    // Jarvis не знает и рецепта — чем на этом сайте нажимают «дальше».
    const tab = await pickTab(params);
    if (!tab) {
      throw new Error("нет подходящей вкладки");
    }
    return {
      tabId: tab.id,
      windowId: tab.windowId,
      title: tab.title,
      url: tab.url,
      audible: Boolean(tab.audible),
    };
  }

  if (action === "page" || action === "probe") {
    const tab = await pickTab(params);
    if (!tab) {
      throw new Error("нет подходящей вкладки");
    }
    // На служебных страницах браузера расширению делать нечего: туда код не
    // впрыснуть, и запрет этот браузерный, а не наш.
    if (!isWebUrl(tab.url)) {
      throw new Error("на служебной странице ничего не нажать");
    }
    const done =
      action === "page"
        ? await inPage(tab, jarvisRunPlan, [params.plan || []], true)
        : await inPage(tab, jarvisProbe, [params.limit || 40], false);
    return { ...done, tabId: tab.id, windowId: tab.windowId, title: tab.title, url: tab.url };
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
