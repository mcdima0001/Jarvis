/**
 * Самопроверка page.js на поддельной странице: node extension/selftest.js
 *
 * Браузера на сервере нет, а ошибиться здесь легче всего: интерпретатор шагов
 * решает, по какой кнопке кликнуть. Поэтому страница подделывается ровно в том
 * объёме, который page.js использует, и проверяется главное — что выбирается
 * нужный элемент, а не соседний.
 *
 * Запускается вручную и из pytest (tests/test_extension_page.py), если в
 * системе есть node. Никаких зависимостей.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/** Элемент страницы: столько, сколько нужно page.js. */
function element(options) {
  const attributes = options.attributes || {};
  const self = {
    tag: options.tag || "button",
    textContent: options.text || "",
    id: options.id || "",
    clicked: 0,
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
    getBoundingClientRect: () => ({ width: options.hidden ? 0 : 100, height: options.hidden ? 0 : 20 }),
    click() {
      self.clicked += 1;
    },
  };
  return self;
}

/** Плеер: video или audio с нужным состоянием. */
function player(options) {
  return {
    tag: options.tag || "video",
    paused: options.paused !== false,
    ended: false,
    muted: Boolean(options.muted),
    volume: options.volume === undefined ? 1 : options.volume,
    currentTime: options.currentTime || 0,
    duration: options.duration || 100,
    readyState: 4,
    played: 0,
    pauses: 0,
    getBoundingClientRect: () => ({ width: 0, height: 0 }),
    async play() {
      if (options.rejects) {
        throw new Error("autoplay");
      }
      this.paused = false;
      this.played += 1;
    },
    pause() {
      this.paused = true;
      this.pauses += 1;
    },
  };
}

/**
 * Страница. Настоящий поиск по селектору тут не нужен: page.js спрашивает
 * либо все плееры, либо все кнопки, либо конкретный `[attr="value"]` и `#id`.
 */
function makeDocument({ players = [], controls = [], bySelector = {} }) {
  const pool = [...controls, ...Object.values(bySelector)];
  const attribute = /^\[([\w-]+)="(.*)"\]$/;
  const identifier = /^#([\w-]+)$/;

  const all = (selector) => {
    if (selector.includes("video")) {
      return players;
    }
    if (selector.includes('[role="button"]') || selector.startsWith("button")) {
      return controls;
    }
    const byAttribute = attribute.exec(selector);
    if (byAttribute) {
      return pool.filter((item) => item.getAttribute(byAttribute[1]) === byAttribute[2]);
    }
    const byId = identifier.exec(selector);
    if (byId) {
      return pool.filter((item) => item.id === byId[1]);
    }
    return bySelector[selector] ? [bySelector[selector]] : [];
  };
  return {
    title: "Тестовая страница",
    querySelectorAll: (selector) => all(selector),
    querySelector: (selector) => all(selector)[0] || null,
  };
}

/** Загрузить page.js в чистом окружении с поддельной страницей. */
function load(document) {
  const source = fs.readFileSync(path.join(__dirname, "page.js"), "utf8");
  const sandbox = {
    document,
    location: { href: "https://example.com/track" },
    getComputedStyle: () => ({ visibility: "visible", display: "block", opacity: "1" }),
  };
  vm.createContext(sandbox);
  vm.runInContext(`${source}\n;globalThis.__api = { jarvisRunPlan, jarvisProbe };`, sandbox);
  return sandbox.__api;
}

const checks = [];
const check = (name, body) => checks.push({ name, body });

check("пауза останавливает то, что играет", async () => {
  const playing = player({ paused: false });
  const api = load(makeDocument({ players: [playing] }));
  const result = await api.jarvisRunPlan([{ media: "pause" }]);
  assert(result.done === "media", `ожидалось media, пришло ${result.done}`);
  assert(playing.pauses === 1, "плеер не остановлен");
});

check("пауза без звука ничего не ломает", async () => {
  const api = load(makeDocument({ players: [player({ paused: true })] }));
  const result = await api.jarvisRunPlan([{ media: "pause" }]);
  assert(result.done === null, "останавливать было нечего");
});

check("отказ автозапуска уводит к кнопке сайта", async () => {
  const button = element({ attributes: { "aria-label": "Воспроизвести" } });
  const api = load(
    makeDocument({ players: [player({ paused: true, rejects: true })], controls: [button] }),
  );
  const result = await api.jarvisRunPlan([
    { media: "play" },
    { label: ["воспроизвести"] },
  ]);
  assert(result.done === "label", `ожидалась кнопка, пришло ${result.done}`);
  assert(button.clicked === 1, "кнопка не нажата");
});

check("«like» не попадает в «dislike»", async () => {
  const dislike = element({ attributes: { "aria-label": "Dislike this video" } });
  const like = element({ attributes: { "aria-label": "Like this video" } });
  const api = load(makeDocument({ controls: [dislike, like] }));
  const result = await api.jarvisRunPlan([{ label: ["like this"] }]);
  assert(result.done === "label", "кнопка не найдена");
  assert(dislike.clicked === 0, "нажат дизлайк — ровно противоположная команда");
  assert(like.clicked === 1, "лайк не нажат");
});

check("подпись сравнивается краем слова", async () => {
  const wrong = element({ attributes: { "aria-label": "Открыть следующую страницу" }, text: "" });
  const right = element({ attributes: { "aria-label": "Следующий трек" } });
  const api = load(makeDocument({ controls: [wrong, right] }));
  await api.jarvisRunPlan([{ label: ["следующий"] }]);
  assert(wrong.clicked === 0, "совпадение серединой строки не считается");
  assert(right.clicked === 1, "кнопка трека не нажата");
});

check("невидимые кнопки не нажимаются", async () => {
  const hidden = element({ attributes: { "aria-label": "Лайк" }, hidden: true });
  const api = load(makeDocument({ controls: [hidden] }));
  const result = await api.jarvisRunPlan([{ label: ["лайк"] }]);
  assert(result.done === null, "нажата скрытая кнопка");
});

check("устаревший селектор уступает следующему варианту", async () => {
  const button = element({ attributes: { "data-test-id": "NEXT" } });
  const api = load(
    makeDocument({ bySelector: { '[data-test-id="NEXT"]': button }, controls: [] }),
  );
  const result = await api.jarvisRunPlan([
    { click: [".ytp-old-button", '[data-test-id="NEXT"]'] },
  ]);
  assert(result.done === "click", "не найден второй селектор");
  assert(button.clicked === 1, "кнопка не нажата");
});

check("кривой селектор не роняет план", async () => {
  const document_ = makeDocument({ players: [player({ paused: false })] });
  document_.querySelector = (selector) => {
    if (selector === "((") {
      throw new Error("SyntaxError");
    }
    return null;
  };
  const api = load(document_);
  const result = await api.jarvisRunPlan([{ click: ["(("] }, { media: "pause" }]);
  assert(result.done === "media", "падение на селекторе съело следующий вариант");
});

check("перемотка двигает время", async () => {
  const video = player({ paused: false, currentTime: 10, duration: 300 });
  const api = load(makeDocument({ players: [video] }));
  await api.jarvisRunPlan([{ media: "forward", seconds: 30 }]);
  assert(video.currentTime === 40, `время ${video.currentTime} вместо 40`);
  await api.jarvisRunPlan([{ media: "back", seconds: 100 }]);
  assert(video.currentTime === 0, "время ушло в минус");
});

check("громкость не выходит за края", async () => {
  const video = player({ paused: false, volume: 0.95 });
  const api = load(makeDocument({ players: [video] }));
  await api.jarvisRunPlan([{ media: "louder", amount: 0.2 }]);
  assert(video.volume === 1, `громкость ${video.volume}`);
});

check("играющий плеер важнее длинного", async () => {
  const advert = player({ paused: false, duration: 15 });
  const movie = player({ paused: true, duration: 5400 });
  const api = load(makeDocument({ players: [movie, advert] }));
  await api.jarvisRunPlan([{ media: "pause" }]);
  assert(advert.pauses === 1 && movie.pauses === 0, "остановлен не тот, что играл");
});

check("неизвестный шаг пропускается", async () => {
  const video = player({ paused: false });
  const api = load(makeDocument({ players: [video] }));
  const result = await api.jarvisRunPlan([{ eval: "alert(1)" }, { media: "pause" }]);
  assert(result.done === "media", "чужой шаг сломал план");
});

check("список кнопок собирается с селекторами", async () => {
  const first = element({ attributes: { "aria-label": "Пауза", "data-test-id": "PAUSE" } });
  const second = element({ attributes: { "aria-label": "Пауза" } });
  const third = element({ text: "  Мне   нравится  ", id: "like" });
  const api = load(makeDocument({ controls: [first, second, third] }));
  const probe = api.jarvisProbe(10);
  assert(probe.controls.length === 2, "повторы подписей не схлопнуты");
  assert(probe.controls[0].sel === '[data-test-id="PAUSE"]', probe.controls[0].sel);
  assert(probe.controls[1].name === "Мне нравится", probe.controls[1].name);
  assert(probe.controls[1].sel === "#like", probe.controls[1].sel);
});

function assert(condition, message) {
  if (!condition) {
    throw new Error(message || "не сошлось");
  }
}

(async () => {
  let failed = 0;
  for (const item of checks) {
    try {
      await item.body();
      console.log(`  ок  ${item.name}`);
    } catch (error) {
      failed += 1;
      console.log(`ОШИБКА ${item.name}: ${error.message}`);
    }
  }
  console.log(failed ? `\nпровалено: ${failed}` : `\nвсё сошлось: ${checks.length}`);
  process.exit(failed ? 1 : 0);
})();
