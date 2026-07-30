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
  const children = options.children || [];
  const self = {
    tag: options.tag || "button",
    textContent: options.text || "",
    id: options.id || "",
    clicked: 0,
    hovered: 0,
    parentElement: null,
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
    getBoundingClientRect: () => ({ width: options.hidden ? 0 : 100, height: options.hidden ? 0 : 20 }),
    // Внутренности строки: по ним page.js ищет кнопку воспроизведения.
    querySelectorAll: () => children,
    // Признак в атрибутах: `[data-test-id*="PLAY" i]` и подобное.
    querySelector: (selector) => {
      const marks = /\[([\w-]+)\*?="([^"]+)"(\s+i)?\]/g;
      for (const match of String(selector).matchAll(marks)) {
        const [, name, value] = match;
        const found = children.find((child) => {
          const own = child.getAttribute(name);
          return own && own.toLowerCase().includes(value.toLowerCase());
        });
        if (found) {
          return found;
        }
      }
      return null;
    },
    dispatchEvent() {
      self.hovered += 1;
      return true;
    },
    click() {
      self.clicked += 1;
      // Настоящая кнопка воспроизведения запускает плеер. Без этого нельзя
      // проверить главное: успех «включи» измеряется звуком, а не нажатием.
      if (options.starts) {
        options.starts.paused = false;
      }
    },
  };
  children.forEach((child) => {
    child.parentElement = self;
  });
  return self;
}

/** Поле ввода: столько, сколько нужно шагу `type`. */
function field(options = {}) {
  const self = {
    tag: "input",
    value: "",
    textContent: "",
    events: [],
    keys: [],
    focused: 0,
    isContentEditable: Boolean(options.editable),
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 200, height: 24 }),
    matches: () => true,
    closest: () => null,
    focus() {
      self.focused += 1;
    },
    dispatchEvent(event) {
      self.events.push(event.type);
      if (event.key) {
        self.keys.push(event.key);
      }
      return true;
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
function makeDocument({ players = [], controls = [], bySelector = {}, fields = [] }) {
  const pool = [...controls, ...Object.values(bySelector)];
  const attribute = /^\[([\w-]+)="(.*)"\]$/;
  const identifier = /^#([\w-]+)$/;

  const all = (selector) => {
    if (selector.includes("video")) {
      return players;
    }
    // Порядок важен: список кнопок в page.js включает input[type="button"].
    if (selector.includes('[role="button"]') || selector.startsWith("button")) {
      return controls;
    }
    if (selector.includes("input")) {
      return fields;
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
    activeElement: null,
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
    // Время тут ни при чём: page.js ждёт, пока сайт запустит плеер, а в
    // подделке он либо запускается сразу, либо не запускается вовсе. Ждать
    // по-настоящему означало бы секунды на каждую проверку.
    setTimeout: (body) => {
      body();
      return 0;
    },
    // Наведение мыши: без него не найти кнопку, которая появляется по hover.
    MouseEvent: class {
      constructor(type, init) {
        this.type = type;
        Object.assign(this, init || {});
      }
    },
    Event: class {
      constructor(type, init) {
        this.type = type;
        Object.assign(this, init || {});
      }
    },
    KeyboardEvent: class {
      constructor(type, init) {
        this.type = type;
        Object.assign(this, init || {});
      }
    },
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

check("«нравится» не попадает в «не нравится»", async () => {
  // Ровно то, что случилось на живой Яндекс Музыке: кнопка дизлайка стояла в
  // разметке раньше, и её подпись содержала «нравится» целиком.
  const dislike = element({ attributes: { "aria-label": "Не нравится" } });
  const like = element({ attributes: { "aria-label": "Нравится" } });
  const api = load(makeDocument({ controls: [dislike, like] }));
  const result = await api.jarvisRunPlan([
    { label: ["нравится"], avoid: ["не нравится"] },
  ]);
  assert(result.done === "label", "кнопка не найдена");
  assert(dislike.clicked === 0, "нажат дизлайк");
  assert(like.clicked === 1, "лайк не нажат");
});

check("кавычки в подписи не мешают", async () => {
  // YouTube пишет «Поставить отметку "Нравится"» — слово в кавычках, и без
  // разбора подписи по словам совпадения не находилось вовсе.
  const dislike = element({ attributes: { "aria-label": 'Поставить отметку "Не нравится"' } });
  const like = element({ attributes: { "aria-label": 'Поставить отметку "Нравится"' } });
  const undo = element({ attributes: { "aria-label": 'Убрать отметку "Нравится"' } });
  const api = load(makeDocument({ controls: [dislike, undo, like] }));
  const result = await api.jarvisRunPlan([
    { label: ["нравится"], avoid: ["не нравится", "убрать"] },
  ]);
  assert(result.done === "label", "кнопка не найдена");
  assert(dislike.clicked === 0 && undo.clicked === 0, "нажата соседняя кнопка");
  assert(like.clicked === 1, "лайк не нажат");
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

check("трек включается кнопкой внутри строки", async () => {
  // Кнопка спрятана до наведения — именно поэтому видимость у неё не
  // проверяется, а перед поиском страница получает mouseover.
  const audio = player({ tag: "audio", paused: true });
  const play = element({
    attributes: { "aria-label": "Воспроизвести" },
    hidden: true,
    starts: audio,
  });
  const row = element({
    attributes: { "aria-label": "Трек Midnight City", role: "row" },
    children: [play],
  });
  const api = load(makeDocument({ controls: [row], players: [audio] }));

  const result = await api.jarvisRunPlan([
    { item: ["midnight city"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.done === "item", `ожидалось item, пришло ${result.done}`);
  assert(result.played === true, "звук пошёл, а в ответе этого нет");
  assert(row.hovered > 0, "на строку не наводились");
  assert(play.clicked === 1, "кнопка воспроизведения не нажата");
  assert(row.clicked === 0, "звук уже идёт — по самой строке нажимать не нужно");
});

check("нажали, а звука нет — так и отвечаем", async () => {
  // Живой случай на Яндекс Музыке: строка нашлась, «Воспроизведение»
  // нажалось, Jarvis сказал «включаю» — и тишина.
  const audio = player({ tag: "audio", paused: true, rejects: true });
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  const row = element({
    attributes: { "aria-label": "Трек Midnight City" },
    children: [play],
  });
  const api = load(makeDocument({ controls: [row], players: [audio] }));

  const result = await api.jarvisRunPlan([
    { item: ["midnight city"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.done === "item", "строку всё же нашли");
  assert(result.played === false, "звука нет, а ответ говорит обратное");
  assert(play.clicked === 1, "кнопку попробовать надо было");
  assert(row.clicked === 1, "кнопка не помогла — надо было нажать строку");
  assert(result.buttons && result.buttons.length, "список кнопок рядом не пришёл");
});

check("название не нашлось — рассказываем, что видно", async () => {
  const row = element({ attributes: { "aria-label": "Трек Something Else" } });
  const api = load(makeDocument({ controls: [row] }));

  const result = await api.jarvisRunPlan([{ item: ["midnight city"], play: true }]);

  assert(result.done === null, "нажалось не то");
  assert(result.saw && result.saw.includes("трек something else"), "не видно, что было на странице");
});

check("без кнопки нажимается строка и дожимается плеер", async () => {
  const row = element({ attributes: { "aria-label": "Трек Midnight City" } });
  const audio = player({ tag: "audio", paused: true });
  const api = load(makeDocument({ controls: [row], players: [audio] }));

  const result = await api.jarvisRunPlan([
    { item: ["midnight city"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.done === "item", "строка не нажата");
  assert(row.clicked === 1, "строку нажать всё же нужно");
  assert(audio.played === 1, "плеер не дожали, а нажатие могло только выбрать трек");
});

check("чужое название не включается", async () => {
  const row = element({ attributes: { "aria-label": "Трек Something Else" } });
  const api = load(makeDocument({ controls: [row] }));
  const result = await api.jarvisRunPlan([{ item: ["midnight city"], hint: [] }]);
  assert(result.done === null, "нажалось не то");
  assert(row.clicked === 0, "нажалось не то");
});

check("самое тесное совпадение побеждает", async () => {
  // Живой случай: у списка треков подпись начинается с первого трека, и
  // выбирался весь список целиком вместо строки.
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  const row = element({
    attributes: { "aria-label": "Трек Midnight City" },
    children: [play],
  });
  const list = element({
    attributes: { "aria-label": "Midnight City 04:04 The Reason 03:53 Ты права 04:07" },
  });
  const api = load(makeDocument({ controls: [list, row] }));

  await api.jarvisRunPlan([{ item: ["midnight city"], hint: ["воспроизв"] }]);

  assert(play.clicked === 1, "нажата кнопка внутри строки");
  assert(list.clicked === 0, "весь список нажимать нельзя");
});

check("название узнаётся по словам, а не только целиком", async () => {
  // Живой случай: «нажми на видео, как я на топлес обманывал всех 10 лет».
  // Подряд эта строка не совпадает с заголовком нигде, а по словам — почти.
  const video = element({
    attributes: { "aria-label": "Как я обманывал ВСЕХ 10 лет | Ян Топлес" },
  });
  const other = element({ attributes: { "aria-label": "Как я строил дом" } });
  const api = load(makeDocument({ controls: [other, video] }));

  const result = await api.jarvisRunPlan([
    { label: ["видео, как я на топлес обманывал всех 10 лет"] },
  ]);

  assert(result.done === "label", `ожидалось label, пришло ${result.done}`);
  assert(video.clicked === 1, "ролик не нажат");
  assert(other.clicked === 0, "нажалось не то видео");
});

check("одного общего слова для совпадения мало", async () => {
  // Иначе «включи видео про котиков» открывало бы первое попавшееся видео.
  const video = element({ attributes: { "aria-label": "Видео о ремонте гаража" } });
  const api = load(makeDocument({ controls: [video] }));

  const result = await api.jarvisRunPlan([{ label: ["видео, как я обманывал всех 10 лет"] }]);

  assert(result.done === null, "нажалось совсем не то");
  assert(video.clicked === 0, "нажалось совсем не то");
});

check("запрет сильнее совпадения по словам", async () => {
  const wrong = element({ attributes: { "aria-label": "Мне не нравится этот трек" } });
  const api = load(makeDocument({ controls: [wrong] }));

  const result = await api.jarvisRunPlan([
    { label: ["мне нравится этот трек"], avoid: ["не нравится"] },
  ]);

  assert(result.done === null, "запрет не сработал");
  assert(wrong.clicked === 0, "нажат дизлайк вместо лайка");
});

check("кнопка без подписи находится по атрибутам", async () => {
  // На многих плеерах в строке только иконка, подписи нет вовсе.
  const play = element({ attributes: { "data-test-id": "PLAY_BUTTON" } });
  const row = element({ attributes: { "aria-label": "Трек Midnight City" }, children: [play] });
  const api = load(makeDocument({ controls: [row] }));

  const result = await api.jarvisRunPlan([{ item: ["midnight city"], hint: ["воспроизв"] }]);

  assert(result.done === "item", "строка не найдена");
  assert(play.clicked === 1, "кнопка по атрибуту не нажата");
});

check("не нашли кнопку — рассказываем, что рядом", async () => {
  const other = element({ attributes: { "aria-label": "Ещё", "data-test-id": "MORE" } });
  const row = element({ attributes: { "aria-label": "Трек Midnight City" }, children: [other] });
  const api = load(makeDocument({ controls: [row] }));

  const result = await api.jarvisRunPlan([{ item: ["midnight city"], hint: ["воспроизв"] }]);

  assert(row.clicked === 1, "строку нажать всё же нужно");
  assert(result.buttons && result.buttons.length === 1, "список кнопок не пришёл");
  assert(result.buttons[0].sel === "MORE", result.buttons[0].sel);
});

check("текст печатается в поле и отправляется", async () => {
  const search = field();
  const api = load(makeDocument({ fields: [search] }));

  const result = await api.jarvisRunPlan([{ type: "don't stop me now", submit: true }]);

  assert(result.done === "type", `ожидалось type, пришло ${result.done}`);
  assert(search.value === "don't stop me now", `в поле ${search.value}`);
  assert(search.focused === 1, "поле не получило фокус");
  assert(search.events.includes("input"), "без события input фреймворки не заметят текст");
  assert(search.keys.includes("Enter"), "Enter не отправлен");
});

check("нет поля — печатать некуда", async () => {
  const api = load(makeDocument({ fields: [] }));
  const result = await api.jarvisRunPlan([{ type: "что-нибудь" }]);
  assert(result.done === null, "печатать было некуда");
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
