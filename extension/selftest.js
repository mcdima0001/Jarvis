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
    // Вложенность: по ней page.js отличает строку списка от обёртки, которая
    // склеила подписи всех строк.
    contains: (other) => {
      let node = other;
      while (node) {
        if (node === self) {
          return true;
        }
        node = node.parentElement;
      }
      return false;
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
      // А сайт при этом сообщает системе, что пошёл другой трек.
      if (options.onPress) {
        options.onPress();
      }
    },
  };
  children.forEach((child) => {
    child.parentElement = self;
  });
  // Текст вместе с потомками — как в настоящем DOM. Это не придирка: подпись
  // внешнего элемента поэтому длиннее подписи внутреннего, и «самый внутренний»
  // отличается от «первого попавшегося».
  Object.defineProperty(self, "textContent", {
    get: () => (options.text || "") + children.map((child) => child.textContent).join(""),
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
function makeDocument({ players = [], controls = [], bySelector = {}, fields = [], page = null, boxes = [] }) {
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
    // Сама страница: по умолчанию длинная и прокручиваемая, как обычный сайт.
    scrollingElement: page || { scrollTop: 100, scrollHeight: 5000, clientHeight: 600 },
    querySelectorAll: (selector) => (selector === "*" ? boxes : all(selector)),
    querySelector: (selector) => all(selector)[0] || null,
  };
}

/** Загрузить page.js в чистом окружении с поддельной страницей. */
function load(document, navigator = {}) {
  const source = fs.readFileSync(path.join(__dirname, "page.js"), "utf8");
  const sandbox = {
    document,
    navigator,
    location: { href: "https://example.com/track" },
    // Прокрутка: столько, сколько нужно шагу `scroll`.
    window: {
      innerHeight: 600,
      scrollY: 100,
      scrollBy(shift) {
        this.scrollY = Math.max(0, Math.min(5000, this.scrollY + shift.top));
      },
    },
    getComputedStyle: (element) => ({
      visibility: "visible",
      display: "block",
      opacity: "1",
      // Своя полоса прокрутки — свойство конкретного блока, а не всех подряд.
      overflowY: (element && element.overflowY) || "visible",
    }),
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

check("в списке видимого — самое близкое, а не самое первое", async () => {
  // Живой случай: первыми на Яндекс Музыке стоят пункты меню, и в лог уезжало
  // «главная; поиск; моя волна…» — по такому списку сказать нечего.
  const menu = ["Главная", "Поиск", "Моя волна", "Концерты", "Коллекция"].map((name) =>
    element({ attributes: { "aria-label": name } }),
  );
  const track = element({ attributes: { "aria-label": "Волшебная — Нервы" } });
  const api = load(makeDocument({ controls: [...menu, track] }));

  const result = await api.jarvisRunPlan([{ item: ["нервы волшебные"], play: true }]);

  assert(result.done === null, "совпадения тут и не должно быть");
  assert(result.saw[0] === "волшебная — нервы", `в начале списка ${result.saw[0]}`);
});

check("уже играет — второй раз не тыкаем", async () => {
  // Живой случай: плеер запускался, но проверка его не видела — фильтр
  // «настоящих плееров» требует длительности, а свежесозданной её ещё нет.
  const audio = { tag: "audio", paused: true, ended: false, muted: false, readyState: 0,
                  duration: 0, currentTime: 0, played: 0,
                  getBoundingClientRect: () => ({ width: 0, height: 0 }),
                  async play() { this.played += 1; this.paused = false; },
                  pause() { this.paused = true; } };
  const play = element({ attributes: { "aria-label": "Воспроизвести" }, starts: audio });
  const row = element({ attributes: { "aria-label": "Трек Волшебная" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [audio] }));

  const result = await api.jarvisRunPlan([
    { item: ["волшебная"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === true, "звук пошёл, а проверка его не увидела");
  assert(row.clicked === 0, "по строке нажимать было незачем — уже играет");
  assert(audio.played === 0, "плеер дожимать было незачем");
});

check("кнопка стала паузой — сайт принял нажатие", async () => {
  // Яндекс Музыка сперва идёт за ссылкой на файл, и звука в первые мгновения
  // нет. Но кнопка уже переименовалась — значит нажатие принято, и тыкать
  // дальше незачем. Именно в этом зазоре Jarvis нажимал строку и уезжал на
  // страницу трека.
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  const row = element({ attributes: { "aria-label": "Трек Волшебная" }, children: [play] });
  // Настоящая кнопка меняет подпись сама; подделке нужно сказать об этом.
  const original = play.click;
  play.click = () => {
    original();
    play.getAttribute = (name) => (name === "aria-label" ? "Пауза" : null);
  };
  const api = load(makeDocument({ controls: [row] }));

  const result = await api.jarvisRunPlan([
    { item: ["волшебная"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === true, "сайт ответил паузой, а мы этого не заметили");
  assert(row.clicked === 0, "нажатие принято — по строке тыкать незачем");
});

check("трек уже играет — кнопку паузы не нажимаем", async () => {
  // «Включи X», когда X играет: нажать кнопку значило бы остановить музыку.
  const pause = element({ attributes: { "aria-label": "Пауза", "data-test-id": "PLAY_BUTTON" } });
  const row = element({ attributes: { "aria-label": "Трек Волшебная" }, children: [pause] });
  const api = load(makeDocument({ controls: [row] }));

  const result = await api.jarvisRunPlan([
    { item: ["волшебная"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === true, "трек играет — это и есть успех");
  assert(pause.clicked === 0, "нажали паузу в ответ на «включи»");
  assert(row.clicked === 0, "по строке тыкать тоже незачем");
});

check("чужой играющий трек за успех не считается", async () => {
  // Живой случай: на выдаче играл посторонний трек. Любое нажатие сходило бы за
  // успех — «звучит же», — хотя нужный трек так и не включился.
  const other = { tag: "audio", paused: false, ended: false, muted: false, readyState: 4,
                  duration: 100, currentTime: 30, src: "blob:other", played: 0,
                  getBoundingClientRect: () => ({ width: 0, height: 0 }),
                  async play() { this.played += 1; },
                  pause() { this.paused = true; } };
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  const row = element({ attributes: { "aria-label": "Трек Волшебная" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [other] }));

  const result = await api.jarvisRunPlan([
    { item: ["волшебная"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === false, "чужой трек сошёл за включённый");
});

check("трек сменился при том же адресе — всё равно включилось", async () => {
  // Яндекс Музыка играет через MediaSource: `src` — это blob:, созданный один
  // раз, и смену трека он **переживает**. Флага паузы тоже мало: музыка играла
  // до нажатия и играет после. Отличают трек длительность и сброс времени.
  const player_ = { tag: "audio", paused: false, ended: false, muted: false, readyState: 4,
                    duration: 202, currentTime: 96, src: "blob:same", played: 0,
                    getBoundingClientRect: () => ({ width: 0, height: 0 }),
                    async play() { this.played += 1; },
                    pause() { this.paused = true; } };
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  const original = play.click;
  play.click = () => {
    original();
    // Тот же blob, играло и играет — меняются только трек и его время.
    player_.duration = 203;
    player_.currentTime = 0;
  };
  const row = element({ attributes: { "aria-label": "Трек Levitating" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [player_] }));

  const result = await api.jarvisRunPlan([
    { item: ["levitating"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === true, "трек сменился, а мы этого не заметили");
  assert(row.clicked === 0, "нажатие принято — по строке тыкать незачем");
});

check("тот же трек играет дальше — это не «включилось»", async () => {
  const player_ = { tag: "audio", paused: false, ended: false, muted: false, readyState: 4,
                    duration: 202, currentTime: 96, src: "blob:same", played: 0,
                    getBoundingClientRect: () => ({ width: 0, height: 0 }),
                    async play() { this.played += 1; },
                    pause() { this.paused = true; } };
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  // Нажатие ничего не меняет: время только идёт вперёд, как при обычной игре.
  const original = play.click;
  play.click = () => { original(); player_.currentTime += 0.2; };
  const row = element({ attributes: { "aria-label": "Трек Levitating" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [player_] }));

  const result = await api.jarvisRunPlan([
    { item: ["levitating"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === false, "чужая музыка сошла за включённый трек");
});

check("сменился трек — значит включилось", async () => {
  const player_ = { tag: "audio", paused: false, ended: false, muted: false, readyState: 4,
                    duration: 100, currentTime: 30, src: "blob:old", played: 0,
                    getBoundingClientRect: () => ({ width: 0, height: 0 }),
                    async play() { this.played += 1; },
                    pause() { this.paused = true; } };
  // Настоящая кнопка переключает источник — подделке скажем об этом.
  const play = element({ attributes: { "aria-label": "Воспроизвести" } });
  const original = play.click;
  play.click = () => {
    original();
    player_.src = "blob:new";
  };
  const row = element({ attributes: { "aria-label": "Трек Волшебная" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [player_] }));

  const result = await api.jarvisRunPlan([
    { item: ["волшебная"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === true, "источник сменился, а мы этого не заметили");
  assert(row.clicked === 0, "нажатие принято — по строке тыкать незачем");
});

check("беззвучный предпросмотр за звук не считается", async () => {
  // На YouTube наведение мыши запускает беззвучный ролик, а наведение — как
  // раз то, что делает шаг item.
  const preview = player({ paused: false, muted: true });
  const row = element({ attributes: { "aria-label": "Трек Волшебная" } });
  const api = load(makeDocument({ controls: [row], players: [preview] }));

  const result = await api.jarvisRunPlan([{ item: ["волшебная"], play: true }]);

  assert(result.played === false, "беззвучное видео сошло за успех");
});

check("страница листается вниз", async () => {
  const document_ = makeDocument({});
  const api = load(document_);

  const result = await api.jarvisRunPlan([{ scroll: "down" }]);

  assert(result.done === "scroll", `ожидался scroll, пришло ${result.done}`);
  assert(document_.scrollingElement.scrollTop > 100, "страница не сдвинулась");
});

check("листается внутренний блок, когда сама страница не двигается", async () => {
  // Живой случай 01.08.2026, Яндекс Музыка: содержимое лежит в блоке со своей
  // полосой, а сама страница неподвижна — `window.scrollY` не меняется ни от
  // чего, и шаг считал себя неудавшимся. «Прокрути страницу ниже» отвечало
  // «не нашёл, чем это сделать», хотя листать было что.
  const still = { scrollTop: 0, scrollHeight: 600, clientHeight: 600 };
  const menu = {
    scrollTop: 0, scrollHeight: 2000, clientHeight: 500, overflowY: "auto",
    getBoundingClientRect: () => ({ width: 200, height: 500 }),
  };
  const main = {
    scrollTop: 0, scrollHeight: 9000, clientHeight: 700, overflowY: "auto",
    getBoundingClientRect: () => ({ width: 1200, height: 700 }),
  };
  const api = load(makeDocument({ page: still, boxes: [menu, main] }));

  const result = await api.jarvisRunPlan([{ scroll: "down" }]);

  assert(result.done === "scroll", `ожидался scroll, пришло ${result.done}`);
  assert(main.scrollTop > 0, "содержимое не сдвинулось");
  assert(menu.scrollTop === 0, "боковое меню трогать не надо — оно меньше");
});

check("листать нечего — это ответ, а не отказ", async () => {
  // Короткая страница без прокрутки: отказ тут читался бы как «команда не
  // работает», хотя всё сработало и двигаться просто некуда.
  const still = { scrollTop: 0, scrollHeight: 600, clientHeight: 600 };
  const api = load(makeDocument({ page: { ...still, scrollHeight: 5000 } }));

  const result = await api.jarvisRunPlan([{ scroll: "bottom" }]);

  assert(result.done === "scroll", `ожидался scroll, пришло ${result.done}`);
});

check("незнакомое направление листания пропускается", async () => {
  const api = load(makeDocument({}));
  const result = await api.jarvisRunPlan([{ scroll: "боком" }]);
  assert(result.done === null, "листать боком мы не умеем");
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

check("обёртка списка не побеждает строку", async () => {
  // Живой случай на Яндекс Музыке: «включи трек Dua Lipa Break My Heart»
  // выбрало «new rulesdua lipa03:29let you break my heart againmorlix…» — всю
  // выдачу целиком. Слов просьбы в ней больше, чем в любой отдельной строке,
  // поэтому одной длины для выбора мало.
  const play = element({ attributes: { "aria-label": "Воспроизведение" } });
  const row = element({
    attributes: { "aria-label": "Break My Heart Dua Lipa" },
    children: [play],
  });
  const wrapper = element({
    attributes: { "aria-label": "New RulesDua Lipa03:29Break My Heart Dua Lipa" },
    children: [row],
  });
  const api = load(makeDocument({ controls: [wrapper, row, play] }));

  const result = await api.jarvisRunPlan([
    { item: ["dua lipa break my heart"], hint: ["воспроизв"], play: true },
  ]);

  assert(wrapper.clicked === 0, "нажалась вся выдача целиком");
  assert(play.clicked === 1, `кнопка внутри строки не нажата (${result.detail})`);
});

check("верхний результат выдачи важнее совпадения слов", async () => {
  // Живой случай: «Dua Lipa Break My Heart» пришло как «2, липа Brake My
  // Heart». Верхней карточкой Яндекс поставил трек Dua Lipa, а совпадение по
  // словам увело в ремиксы посторонних артистов — «break» и «heart» есть и там.
  const audio = player({ tag: "audio", paused: true });
  const cover = element({
    attributes: { class: "PlayButtonWithCover_playButton__rV9pQ" },
    hidden: true,
    starts: audio,
  });
  const card = element({ attributes: { "aria-label": "Love Again" }, children: [cover] });
  const remix = element({ attributes: { "aria-label": "you broke my heart" } });
  const api = load(
    makeDocument({
      controls: [card, cover, remix],
      bySelector: { '[class*="PlayButtonWithCover_playButton"]': cover },
      players: [audio],
    }),
  );

  const result = await api.jarvisRunPlan([
    {
      item: ["dua lipa break my heart"],
      hint: ["воспроизв"],
      play: true,
      prefer: ['[class*="PlayButtonWithCover_playButton"]'],
    },
  ]);

  assert(result.played === true, `не заиграло (${result.detail})`);
  assert(remix.clicked === 0, "нажался ремикс вместо верхней карточки");
  assert(cover.clicked === 1, "кнопка верхней карточки не нажата");
  assert(result.detail.includes("верхний результат"), `в логе ${result.detail}`);
});

check("нет верхнего результата — сравниваем названия", async () => {
  const row = element({ attributes: { "aria-label": "Трек Midnight City" } });
  const api = load(makeDocument({ controls: [row] }));

  const result = await api.jarvisRunPlan([
    { item: ["midnight city"], prefer: ['[class*="НетТакого"]'] },
  ]);

  assert(result.done === "item", "название искать всё равно надо");
  assert(row.clicked === 1, "строка не нажата");
});

check("голый адрес не считается названием", async () => {
  // Живой случай: на странице висел текст прошлого разговора, а в нём ссылка
  // с запросом. Слова просьбы нашлись в ней все, и она «совпала» лучше трека.
  const link = element({
    tag: "a",
    attributes: { "aria-label": "https://www.youtube.com/results?search_query=dua+lipa+-+break+my+heart" },
  });
  const api = load(makeDocument({ controls: [link] }));

  const result = await api.jarvisRunPlan([{ item: ["dua lipa break my heart"], play: true }]);

  assert(result.done === null, "нажалась ссылка из текста");
  assert(link.clicked === 0, "нажалась ссылка из текста");
});

check("пустая страница отличается от полной без совпадений", async () => {
  // Пустой список в лог не попадал вовсе, и четыре попытки подряд не оставляли
  // ни строки: выглядело как «Jarvis молча передумал».
  const empty = load(makeDocument({}));
  const nothing = await empty.jarvisRunPlan([{ item: ["midnight city"], play: true }]);
  assert(nothing.counted === 0, `на пустой странице counted=${nothing.counted}`);

  const row = element({ attributes: { "aria-label": "Совсем другой трек" } });
  const full = load(makeDocument({ controls: [row] }));
  const missed = await full.jarvisRunPlan([{ item: ["midnight city"], play: true }]);
  assert(missed.counted === 1, `элементы были, а counted=${missed.counted}`);
});

check("иконка внутри кнопки не роняет план", async () => {
  // Живой случай, стоивший нескольких заходов разбора: признак «похоже на play»
  // совпал с <svg class="…playButtonIcon…"> внутри кнопки, а у SVG нет click().
  // Вызов свалился, план молча оборвался, и со стороны это выглядело как «на
  // странице ничего не нашлось».
  const audio = player({ tag: "audio", paused: true });
  const icon = element({ tag: "svg", attributes: { class: "playButtonIcon" } });
  icon.click = undefined;                       // у SVG его и нет
  const button = element({
    attributes: { class: "playButton" },
    children: [icon],
    starts: audio,
  });
  icon.parentElement = button;
  const row = element({ attributes: { "aria-label": "Трек Волшебная" }, children: [button] });
  const api = load(makeDocument({ controls: [row], players: [audio] }));

  const result = await api.jarvisRunPlan([{ item: ["волшебная"], play: true }]);

  assert(result.done === "item", `план оборвался: ${JSON.stringify(result)}`);
  assert(!result.broke, `в ответе ошибка: ${result.broke}`);
});

check("сломавшийся шаг рассказывает, на чём споткнулся", async () => {
  // Молчаливый обрыв — худшее из возможного: он неотличим от «не нашлось».
  const api = load(makeDocument({}));
  api.jarvisRunPlan.length;                     // просто чтобы функция была
  const broken = { item: ["трек"], play: true };
  const document_ = makeDocument({});
  document_.querySelectorAll = () => {
    throw new Error("страница сопротивляется");
  };
  const angry = load(document_);

  const result = await angry.jarvisRunPlan([broken]);

  assert(result.done === null, "сработать тут было нечему");
  assert(result.broke && result.broke.length === 1, "ошибка не попала в ответ");
  assert(String(result.broke[0]).includes("сопротивляется"), result.broke[0]);
});

check("исправление запроса нажимается за последнюю часть", async () => {
  // Живой случай: распознавание услышало «нервы, волшануя», и Яндекс Музыка
  // предложила «Возможно, вы искали нервы, волшебная». Нажать надо исправленный
  // запрос — он в конце строки и оформлен ссылкой.
  const fix = element({ tag: "a", attributes: { href: "/search?text=x" }, text: "нервы, волшебная" });
  const banner = element({
    tag: "div",
    text: "Возможно, вы искали ",
    children: [fix],
  });
  const api = load(makeDocument({ controls: [banner, fix] }));

  const result = await api.jarvisRunPlan([{ suggest: ["возможно, вы искали"] }]);

  assert(result.done === "suggest", `ожидался suggest, пришло ${result.done}`);
  assert(fix.clicked === 1, "нажата не исправленная часть");
  assert(banner.clicked === 0, "весь баннер нажимать не надо");
});

check("самый внутренний баннер, а не тело страницы", async () => {
  // Обход идёт от внешних к внутренним, и первым под приметное слово попадает
  // страница целиком — а «последняя ссылка внутри неё» это ссылка в подвале.
  const footer = element({ tag: "a", attributes: { href: "/about" }, text: "правообладателям" });
  const fix = element({ tag: "a", attributes: { href: "/search?text=x" }, text: "нервы, волшебная" });
  const banner = element({ tag: "div", text: "Возможно, вы искали ", children: [fix] });
  const body = element({ tag: "div", text: "Возможно, вы искали ", children: [banner, footer] });
  const api = load(makeDocument({ controls: [body, banner, fix, footer] }));

  await api.jarvisRunPlan([{ suggest: ["возможно, вы искали"] }]);

  assert(fix.clicked === 1, "нажата не исправленная часть");
  assert(footer.clicked === 0, "нажата ссылка из подвала");
});

check("нет подсказки — шаг не срабатывает", async () => {
  const row = element({ attributes: { "aria-label": "Трек Волшебная" } });
  const api = load(makeDocument({ controls: [row] }));
  const result = await api.jarvisRunPlan([{ suggest: ["возможно, вы искали"] }]);
  assert(result.done === null, "нажалось что-то посторонее");
  assert(row.clicked === 0, "нажалось что-то посторонее");
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

check("сайт сам сказал, что играет — верим ему, а не разметке", async () => {
  // Живой случай 01.08.2026: на Яндекс Музыке трек включался, а ответ был
  // «включить не получилось». Плеер сайта в разметке не находится вовсе, и всё,
  // что опирается на <audio>, обречено молчать. Зато сайт сообщает системе
  // название трека — тем же путём, каким оно попадает во всплывашку Windows.
  // Этот источник от разметки не зависит, и верить надо ему.
  const media = { metadata: { title: "Старый трек", artist: "Кто-то" }, playbackState: "playing" };
  const play = element({
    attributes: { "aria-label": "Воспроизведение" },
    onPress: () => {
      media.metadata = { title: "Levitating", artist: "Dua Lipa" };
    },
  });
  const row = element({ attributes: { "aria-label": "Трек Levitating" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [] }), { mediaSession: media });

  const result = await api.jarvisRunPlan([
    { item: ["levitating"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === true, "сменившееся название — это и есть «включилось»");
});

check("название не менялось — значит не включилось", async () => {
  const media = { metadata: { title: "Старый трек", artist: "Кто-то" }, playbackState: "playing" };
  const play = element({ attributes: { "aria-label": "Воспроизведение" } });
  const row = element({ attributes: { "aria-label": "Трек Levitating" }, children: [play] });
  const api = load(makeDocument({ controls: [row], players: [] }), { mediaSession: media });

  const result = await api.jarvisRunPlan([
    { item: ["levitating"], hint: ["воспроизв"], play: true },
  ]);

  assert(result.played === false, "играет то же самое — успехом это не считается");
  assert(String(result.heard).includes("Старый трек"), "в лог уходит то, что слышали");
});

check("не сработало ничего — говорим, что слышала страница", async () => {
  // Живой случай 01.08.2026: «перемотай на 30 секунд» на Яндекс Музыке.
  // Перемотка опирается на сам плеер, а его в разметке нет — и ответ был
  // неотличим от «команда не подошла». «Плееров 0» объясняет это мгновенно.
  const media = { metadata: { title: "Трек", artist: "Кто-то" }, playbackState: "playing" };
  const api = load(makeDocument({ players: [] }), { mediaSession: media });

  const result = await api.jarvisRunPlan([{ media: "forward", seconds: 30 }]);

  assert(result.done === null, "перематывать было нечем");
  assert(String(result.heard).includes("плееров 0"), result.heard);
});

check("перемотка ползунком, когда плеера в разметке нет", async () => {
  // Яндекс Музыка играет через `new Audio()`, не вставленный в документ:
  // querySelectorAll его не находит никогда. Зато полоса перемотки — обычный
  // input[type="range"], и в нём есть всё: value это секунда, max — длина.
  const slider = field();
  slider.max = "194";
  slider.value = "183";
  slider.getAttribute = (name) => (name === "aria-label" ? "Таймкод" : null);
  const api = load(makeDocument({ players: [], bySelector: { '[aria-label="Таймкод"]': slider } }));

  const result = await api.jarvisRunPlan([{ range: ['[aria-label="Таймкод"]'], by: 10 }]);

  assert(result.done === "range", `ожидался range, пришло ${result.done}`);
  assert(slider.value === "193", `ожидалось 193, стало ${slider.value}`);
  assert(slider.events.includes("input"), "сайт не узнает о сдвиге без события input");
});

check("перемотка не уезжает за края трека", async () => {
  const slider = field();
  slider.id = "seek";
  slider.max = "194";
  slider.value = "190";
  const api = load(makeDocument({ players: [], bySelector: { "#seek": slider } }));

  await api.jarvisRunPlan([{ range: ["#seek"], by: 30 }]);

  assert(slider.value === "194", `конец трека — это конец: ${slider.value}`);
});

check("ползунок находится и без селектора — по подписи", async () => {
  // Живой случай 01.08.2026: селектор из рецепта снят с плеера «Моей волны»
  // («Управление таймкодом»), а на обычном треке плеер другой — и шаг не
  // срабатывал вовсе. Селектор сайта тут подсказка, а не условие.
  const volume = field();
  volume.max = "1";
  volume.getAttribute = (name) => (name === "aria-label" ? "Громкость" : null);
  const seek = field();
  seek.max = "194";
  seek.value = "100";
  seek.getAttribute = (name) => (name === "aria-label" ? "Управление таймкодом" : null);
  const api = load(makeDocument({ players: [], fields: [volume, seek] }));

  const result = await api.jarvisRunPlan([{ range: ["[data-test-id=\"NOPE\"]"], by: 10 }]);

  assert(result.done === "range", `ожидался range, пришло ${result.done}`);
  assert(seek.value === "110", `сдвинуть надо перемотку: ${seek.value}`);
  assert(volume.value === "", "громкость трогать нельзя");
});

check("без подписи ползунок выбирается по длине шкалы", async () => {
  // У громкости шкала 0..1, у перемотки — секунды трека. Перепутать трудно.
  const volume = field();
  volume.max = "1";
  const seek = field();
  seek.max = "194";
  seek.value = "50";
  const api = load(makeDocument({ players: [], fields: [volume, seek] }));

  await api.jarvisRunPlan([{ range: ["#seek"], by: -20 }]);

  assert(seek.value === "30", `ожидалось 30, стало ${seek.value}`);
});

check("нет ползунка — шаг не срабатывает, но список уходит в лог", async () => {
  const api = load(makeDocument({ players: [] }));

  const result = await api.jarvisRunPlan([{ range: ["#seek"], by: 10 }]);

  assert(result.done === null, "нечего было двигать");
  assert(Array.isArray(result.sliders), "какие ползунки есть — обязано попасть в ответ");
});

check("говорит, что играет, по данным сайта", async () => {
  // `navigator.mediaSession` — то самое, из чего Windows рисует всплывашку по
  // кнопке «play». Разметки это не касается, поэтому работает и там, где плеера
  // в документе нет вовсе.
  const media = { metadata: { title: "Shame", artist: "Joseph Angel" }, playbackState: "playing" };
  const api = load(makeDocument({ players: [] }), { mediaSession: media });

  const result = await api.jarvisRunPlan([{ now: "track" }]);

  assert(result.done === "now", `ожидался now, пришло ${result.done}`);
  assert(result.detail === "Shame — Joseph Angel", result.detail);
});

check("без исполнителя название не обрастает тире", async () => {
  const media = { metadata: { title: "Подкаст" }, playbackState: "playing" };
  const api = load(makeDocument({ players: [] }), { mediaSession: media });

  const result = await api.jarvisRunPlan([{ now: "track" }]);

  assert(result.detail === "Подкаст", result.detail);
});

check("сайт молчит — берём заголовок вкладки, но только пока звук идёт", async () => {
  // У ютуба заголовок вкладки и есть название ролика. Проверка на звук
  // обязательна: иначе в ответ уедет название любой открытой страницы.
  const quiet = load(makeDocument({ players: [player({ paused: true })] }), { mediaSession: {} });
  assert((await quiet.jarvisRunPlan([{ now: "track" }])).done === null, "в тишине называть нечего");

  const loud = load(makeDocument({ players: [player({ paused: false })] }), { mediaSession: {} });
  const result = await loud.jarvisRunPlan([{ now: "track" }]);

  assert(result.done === "now", `ожидался now, пришло ${result.done}`);
  assert(result.detail === "Тестовая страница", result.detail);
});
