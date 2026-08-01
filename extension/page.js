/**
 * Что Jarvis умеет делать внутри страницы.
 *
 * Здесь лежат две функции, которые расширение впрыскивает во вкладку через
 * `chrome.scripting.executeScript({ func })`. Впрыскивается именно **код этого
 * файла**, а из Jarvis приходят только данные: список шагов и слов. Это
 * принципиально. Голос — недоверенный ввод: он проходит путь микрофон →
 * Whisper → LLM → аргумент, и превращать его в исполняемый код нельзя ни на
 * одном шаге. Селектор уходит в `querySelector`, подпись — в сравнение строк;
 * ни то, ни другое ничего выполнить не может.
 *
 * Обе функции обязаны быть самодостаточными: наружу отправляется их исходный
 * текст, поэтому ничего из окружения расширения внутри не существует.
 *
 * План — это список **вариантов**, а не последовательность. Выполняется первый
 * сработавший: сначала точный селектор конкретного сайта, потом кнопка по
 * подписи, потом сам плеер. Разметку сайты меняют регулярно, поэтому запасной
 * вариант тут не роскошь, а условие того, что команда переживёт редизайн.
 */

/**
 * Выполнить план в текущей странице.
 *
 * @param {Array<Object>} plan шаги-варианты: {media}, {label}, {click}, {item},
 *   {type}, {scroll}, {suggest}.
 * @returns {Promise<Object>} что сработало: {done, detail, title, url}.
 */
async function jarvisRunPlan(plan) {
  const MAX_STEPS = 8;
  const CLICKABLE =
    'button, [role="button"], [role="menuitem"], [role="switch"], a[href], ' +
    'input[type="button"], input[type="submit"]';

  /** Видно ли элемент. Нулевой размер и `display: none` — это не кнопка. */
  const seen = (element) => {
    if (!element || !element.getBoundingClientRect) {
      return false;
    }
    const box = element.getBoundingClientRect();
    if (!box.width || !box.height) {
      return false;
    }
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  };

  /**
   * Плееры страницы. Видимость тут не проверяется намеренно: Яндекс Музыка
   * играет через <audio>, у которого размера нет вовсе.
   */
  const players = () =>
    Array.from(document.querySelectorAll("video, audio")).filter(
      (item) => item.readyState > 0 || item.currentTime > 0 || item.duration > 0,
    );

  /** Тот плеер, о котором говорят: играющий, а из молчащих — самый длинный. */
  const mainPlayer = () => {
    const all = players();
    const playing = all.filter((item) => !item.paused && !item.ended);
    const pool = playing.length ? playing : all;
    return pool.sort((first, second) => (second.duration || 0) - (first.duration || 0))[0] || null;
  };

  /** Ёлки-палки регистра и «ё»: сравнивать подписи иначе бессмысленно. */
  const norm = (text) => String(text || "").toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim();

  /** Подпись кнопки так, как её читает человек и озвучивает экранный диктор. */
  const caption = (element) =>
    norm(element.getAttribute("aria-label") || element.getAttribute("title") || element.textContent);

  const media = async (what, step) => {
    const playing = players().filter((item) => !item.paused && !item.ended);

    if (what === "pause" || (what === "toggle" && playing.length)) {
      if (!playing.length) {
        return null;
      }
      playing.forEach((item) => item.pause());
      return "пауза";
    }

    if (what === "play" || what === "toggle") {
      const target = mainPlayer();
      if (!target) {
        return null;
      }
      try {
        // Браузер вправе отказать: автозапуск со звуком разрешён только там,
        // где пользователь уже что-то включал руками. Отказ — не ошибка, а
        // повод попробовать следующий вариант, то есть нажать кнопку сайта.
        await target.play();
      } catch (error) {
        return null;
      }
      return "воспроизведение";
    }

    const target = mainPlayer();
    if (!target) {
      return null;
    }

    if (what === "mute" || what === "unmute") {
      target.muted = what === "mute";
      return what === "mute" ? "звук выключен" : "звук включён";
    }

    if (what === "louder" || what === "quieter") {
      const step_ = Number(step.amount) > 0 ? Number(step.amount) : 0.1;
      const shift = what === "louder" ? step_ : -step_;
      target.muted = false;
      target.volume = Math.min(1, Math.max(0, target.volume + shift));
      return `громкость ${Math.round(target.volume * 100)}%`;
    }

    if (what === "forward" || what === "back") {
      const seconds = Number(step.seconds) > 0 ? Number(step.seconds) : 15;
      const shift = what === "forward" ? seconds : -seconds;
      const limit = target.duration || Number.MAX_SAFE_INTEGER;
      target.currentTime = Math.min(limit, Math.max(0, target.currentTime + shift));
      return `${Math.round(target.currentTime)} с`;
    }

    return null;
  };

  /**
   * Подпись, разложенная по словам: всё, кроме букв и цифр, становится
   * границей. Иначе кавычки в «Поставить отметку "Нравится"» приклеиваются к
   * слову, и совпадения по нему не находится вовсе.
   */
  const spaced = (text) => ` ${norm(text).replace(/[^\p{L}\p{N}]+/gu, " ").trim()} `;

  /**
   * Совпадение началом слова, а не любым куском. «like» внутри «dislike» и
   * «нравится» внутри «не нравится» — ровно противоположные команды.
   */
  const hits = (label, word) => label.includes(` ${word}`);

  /** Слова просьбы, по которым есть смысл искать: короткие не значат ничего. */
  const keywords = (text) => text.split(" ").filter((word) => word.length >= 3);

  /**
   * Насколько подпись отвечает просьбе: 1 — просьба нашлась целиком, 0 — нет.
   *
   * Целиком совпадает далеко не всё. Название ролика проходит путь микрофон →
   * Whisper → шаблон фразы, и обрастает по дороге лишними словами: «нажми на
   * видео, как я на топлес обманывал всех 10 лет» против «Как я обманывал ВСЕХ
   * 10 лет | Ян Топлес» на странице. Подряд эти строки не совпадают нигде, а по
   * словам — почти полностью. Поэтому вторым заходом считается доля слов
   * просьбы, найденных в подписи; порог высокий, и одного слова не хватает
   * никогда — иначе «видео» открывало бы первое попавшееся.
   */
  const ENOUGH = 0.6;
  const score = (label, wanted) => {
    if (hits(label, wanted)) {
      return 1;
    }
    const words = keywords(wanted);
    if (words.length < 2) {
      return 0;
    }
    const found = words.filter((word) => hits(label, word)).length;
    const share = found / words.length;
    return found >= 2 && share >= ENOUGH ? share * 0.9 : 0;
  };

  /** Лучшее совпадение подписи с просьбой среди элементов страницы. */
  const bestMatch = (selector, wanted, forbidden, tighter) => {
    const found = [];
    for (const element of document.querySelectorAll(selector)) {
      if (!seen(element)) {
        continue;
      }
      const caption_ = caption(element);
      // Длиннее этого — уже кусок страницы, а не подпись.
      if (!caption_ || caption_.length > 200) {
        continue;
      }
      // Голый адрес — не название. На странице с текстом прошлого разговора
      // ссылка «youtube.com/results?search_query=dua+lipa+-+break+my+heart»
      // содержала все слова просьбы и «совпала» лучше любого трека.
      if (/^https?:\/\/\S+$/.test(caption_)) {
        continue;
      }
      const label = spaced(caption_);
      // Запрет сильнее совпадения: «не нравится» содержит «нравится» целиком,
      // и без этой проверки лайк превращается в дизлайк.
      if (forbidden.some((word) => hits(label, word))) {
        continue;
      }
      const rank = Math.max(...wanted.map((word) => score(label, word)));
      if (rank > 0) {
        found.push({ element, rank, size: label.length });
      }
    }
    if (!found.length) {
      return null;
    }

    // Обёртка списка склеивает подписи всех строк подряд и поэтому отвечает
    // просьбе **лучше** любой отдельной строки: слов в ней больше. Живой
    // случай на Яндекс Музыке — «включи трек Dua Lipa Break My Heart» выбрало
    // «new rulesdua lipa03:29let you break my heart againmorlix…», то есть всю
    // выдачу целиком. Одной длины тут мало: у обёртки и совпадение выше.
    // Поэтому элемент, внутри которого лежит другой подходящий, отбрасывается —
    // побеждает самый внутренний.
    const inner = found.filter(
      (one) =>
        !found.some(
          (other) =>
            other !== one &&
            typeof one.element.contains === "function" &&
            one.element.contains(other.element),
        ),
    );
    const pool = inner.length ? inner : found;

    // При равном совпадении порядок решает вызывающий: у кнопок побеждает
    // первая на странице (у видео это лайк ролика, а не лайк комментария),
    // у строк списка — самая короткая подпись.
    let best = pool[0];
    for (const item of pool.slice(1)) {
      if (item.rank > best.rank || (tighter && item.rank === best.rank && item.size < best.size)) {
        best = item;
      }
    }
    return best.element;
  };

  /** Кнопка, чья подпись отвечает просьбе и не содержит запретного. */
  const byLabel = (step) => {
    const wanted = step.label.map((word) => spaced(word).trim()).filter(Boolean);
    const forbidden = (step.avoid || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!wanted.length) {
      return null;
    }
    return bestMatch(CLICKABLE, wanted, forbidden, false);
  };

  /**
   * Нажать исправление запроса: «Возможно, вы искали <правильно>».
   *
   * Распознавание пишет названия как слышит, и сайт сам предлагает поправку.
   * Нажать нужно **исправленный запрос**, а не весь баннер: правильная часть
   * стоит в конце строки и обычно оформлена ссылкой.
   *
   * Из подходящих элементов берётся тот, у кого подпись **короче всего**. Обход
   * идёт от внешних к внутренним, и первым под приметное слово попадает тело
   * страницы целиком — а «последняя ссылка внутри него» это ссылка в подвале.
   */
  const suggestion = (step) => {
    const marks = (step.suggest || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!marks.length) {
      return null;
    }
    const WHERE = 'a[href], button, [role="button"], [role="link"], div, p, span';
    let banner = null;
    let tightest = Infinity;
    for (const element of document.querySelectorAll(WHERE)) {
      if (!seen(element)) {
        continue;
      }
      const text = caption(element);
      if (!text || text.length > 200) {
        continue;
      }
      const label = spaced(text);
      if (marks.some((word) => hits(label, word)) && text.length < tightest) {
        tightest = text.length;
        banner = element;
      }
    }
    if (!banner) {
      return null;
    }
    const inside = Array.from(banner.querySelectorAll(CLICKABLE)).filter(seen);
    const target = inside.length ? inside[inside.length - 1] : banner;
    return press(target) ? caption(target) || caption(banner) : null;
  };

  /**
   * Пролистать страницу.
   *
   * Кнопки для этого нет ни на одном сайте, а просьба обычная. Живой случай:
   * «пролистай страницу вверх» модель разобрала как перемотку назад — из всего
   * каталога это было самое близкое.
   */
  const scrollPage = (step) => {
    const where = String(step.scroll || "").toLowerCase();
    const height = window.innerHeight || 600;
    const moves = {
      down: [0, height * 0.9],
      up: [0, -height * 0.9],
      top: [0, -1e7],
      bottom: [0, 1e7],
    };
    const move = moves[where];
    if (!move) {
      return null;
    }
    const before = window.scrollY;
    window.scrollBy({ left: move[0], top: move[1], behavior: "instant" });
    // Страница могла быть уже в самом низу — тогда листать было нечего, и это
    // не успех: пусть план идёт дальше, вдруг у сайта своя кнопка.
    return window.scrollY === before && where !== "top" && where !== "bottom" ? null : where;
  };

  /**
   * Напечатать текст в поле на странице — например в поиск сайта.
   *
   * Это **не** эмуляция клавиатуры на уровне системы: текст кладётся в
   * конкретное поле конкретной вкладки и никуда больше уйти не может. Значение
   * ставится через родной сеттер, иначе React с Vue его не замечают: они
   * следят за свойством, а не за атрибутом.
   */
  /**
   * Присвоить значение полю так, чтобы сайт это заметил.
   *
   * Простое `field.value = …` React и Vue **пропускают**: они следят за родным
   * сеттером прототипа, а присваивание свойству экземпляра его обходит. Нужен
   * именно тот сеттер, который перекрыт фреймворком.
   */
  const setValue = (field, value) => {
    try {
      const proto =
        typeof HTMLTextAreaElement !== "undefined" && field instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement
          : HTMLInputElement;
      const own = Object.getOwnPropertyDescriptor(proto.prototype, "value");
      if (own && own.set) {
        own.set.call(field, value);
        return;
      }
    } catch (error) {
      // Прототипа под рукой нет — присвоим напрямую, хуже не будет.
    }
    field.value = value;
  };

  /**
   * Сдвинуть ползунок сайта: перемотка и громкость там, где плеера нет.
   *
   * Это не запасной вариант, а **единственный** для целого класса сайтов.
   * Яндекс Музыка играет через `new Audio()`, не вставленный в документ, —
   * `document.querySelectorAll("video, audio")` не находит его никогда, и всё,
   * что опирается на плеер, там мертво. Зато полоса перемотки у них — обычный
   * `input[type="range"]`, и в нём есть **всё нужное**: `value` это текущая
   * секунда, `max` — длительность.
   *
   * Поэтому шаг не «нажми куда-то», а «прибавь столько-то»: сайт сам знает,
   * где сейчас трек, а нам остаётся арифметика.
   */
  const slide = (step) => {
    const found = bySelector(step.range);
    if (!found) {
      return null;
    }
    const input = found.element;
    const by = Number(step.by || 0);
    const max = Number(input.max);
    const min = Number(input.min || 0);
    if (!by || !Number.isFinite(max) || !(max > min)) {
      return null;
    }
    const now = Number(input.value || 0) || 0;
    const target = Math.max(min, Math.min(max, now + by));
    setValue(input, String(target));
    for (const name of ["input", "change"]) {
      try {
        input.dispatchEvent(new Event(name, { bubbles: true }));
      } catch (error) {
        // Событие не прошло — значение всё равно на месте.
      }
    }
    return { detail: `${Math.round(target)} из ${Math.round(max)}`, selector: found.selector };
  };

  const typeInto = (step) => {
    const FIELDS =
      'input[type="search"], input[type="text"], input:not([type]), textarea, ' +
      '[role="searchbox"], [role="combobox"] input, [contenteditable="true"]';
    const text = String(step.type == null ? "" : step.type);
    if (!text) {
      return null;
    }

    let field = null;
    // Поле уже в фокусе — печатаем в него: обычно пользователь только что сам
    // нажал «поиск», и открылось именно оно.
    const active = document.activeElement;
    if (active && active.matches && seen(active)) {
      try {
        field = active.matches(FIELDS) ? active : null;
      } catch (error) {
        field = null;
      }
    }
    const where = Array.isArray(step.into) && step.into.length ? step.into : [FIELDS];
    for (const selector of where) {
      if (field) {
        break;
      }
      try {
        field = Array.from(document.querySelectorAll(String(selector))).find(seen) || null;
      } catch (error) {
        continue;
      }
    }
    if (!field) {
      return null;
    }

    if (field.focus) {
      field.focus();
    }
    if (field.isContentEditable) {
      field.textContent = text;
    } else {
      setValue(field, text);
    }
    for (const name of ["input", "change"]) {
      try {
        field.dispatchEvent(new Event(name, { bubbles: true }));
      } catch (error) {
        // Событие не прошло — значение всё равно на месте.
      }
    }

    if (step.submit) {
      for (const name of ["keydown", "keypress", "keyup"]) {
        try {
          field.dispatchEvent(
            new KeyboardEvent(name, {
              key: "Enter",
              code: "Enter",
              keyCode: 13,
              which: 13,
              bubbles: true,
            }),
          );
        } catch (error) {
          // Нажатие не прошло — ниже попробуем отправить форму.
        }
      }
      const form = field.form || (field.closest ? field.closest("form") : null);
      if (form && form.requestSubmit) {
        try {
          form.requestSubmit();
        } catch (error) {
          // Форма отказалась — поле всё равно заполнено, видно на экране.
        }
      }
    }
    return text;
  };

  /**
   * Нажать элемент, чем бы он ни оказался.
   *
   * `click()` есть у HTML-элементов, но не у SVG: у иконки внутри кнопки его
   * может не быть вовсе. Живой случай стоил нескольких заходов разбора: признак
   * «похоже на play» в атрибутах совпал с `<svg class="…playButtonIcon…">`
   * внутри кнопки, вызов свалился с ошибкой, и **весь план молча оборвался** —
   * со стороны это выглядело как «на странице ничего не нашлось».
   */
  const press = (element) => {
    try {
      if (typeof element.click === "function") {
        element.click();
        return true;
      }
    } catch (error) {
      // Ниже попробуем событием.
    }
    try {
      element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      return true;
    } catch (error) {
      return false;
    }
  };

  /**
   * Идёт ли звук прямо сейчас. Успех «включи» измеряется этим, а не нажатием.
   *
   * Спрашивается у **всех** плееров страницы, а не у тех, что прошли фильтр
   * `players()`. Это не мелочь: тот фильтр требует `readyState`, длительности
   * или сдвинутого времени, а свежесозданный плеер ничего этого ещё не имеет —
   * и «уже играет» читалось как тишина. Ровно на этом Jarvis нажимал трек,
   * не верил, что получилось, и шёл тыкать дальше.
   *
   * `muted` тоже считается тишиной: на YouTube наведение мыши запускает беззвучный
   * предпросмотр ролика, а наведение — как раз то, что делает `playItem`.
   */
  const sounding = () =>
    Array.from(document.querySelectorAll("video, audio")).some(
      (item) => !item.paused && !item.ended && !item.muted,
    ) || nowPlaying().state === "playing";

  /**
   * Что сайт сам сообщает системе о текущем треке.
   *
   * Это тот же источник, из которого Windows рисует всплывашку при нажатии на
   * кнопку «play» на клавиатуре, и он **надёжнее плееров в разметке**. Причина
   * простая: `document.querySelectorAll("video, audio")` находит не всё. Плеер
   * бывает спрятан в теневом дереве, живёт в служебном кадре или создаётся
   * заново на каждый трек — и тогда список пуст, `sounding()` всегда ложь, а
   * «включить не получилось» звучит при играющей музыке. Ровно это и было
   * поймано в логе 01.08.2026: нажатие проходило, трек играл, ответ был отказом.
   *
   * Название трека вдобавок отвечает на вопрос, на который состояние плеера не
   * отвечает вовсе: **сменился ли трек**. Другое название — значит пошёл новый.
   */
  const nowPlaying = () => {
    try {
      const media = navigator.mediaSession;
      const data = (media && media.metadata) || null;
      return {
        title: data ? `${data.title || ""} — ${data.artist || ""}`.trim() : "",
        state: (media && media.playbackState) || "",
      };
    } catch (error) {
      return { title: "", state: "" };
    }
  };

  /**
   * Слепок состояния плееров: по нему видно, **сменилось ли** то, что играет.
   *
   * Одного «идёт ли звук» мало, когда звук уже идёт: на выдаче мог играть
   * посторонний трек, и любое нажатие сошло бы за успех — «звучит же».
   *
   * Сравнивать при этом адрес источника **бесполезно**, и это стоило ещё одного
   * захода. Яндекс Музыка играет через MediaSource: `src` — это `blob:`, который
   * создаётся один раз и **переживает смену трека**. Флага паузы тоже мало —
   * музыка играла до нажатия и играет после. Работают два других признака:
   * длительность (у другого трека она другая) и время воспроизведения (у нового
   * трека оно сбрасывается к началу, то есть идёт назад).
   */
  const snapshot = () => ({
    // Название текущего трека по версии самого сайта. Живёт отдельно от
    // разметки и переживает и перерисовку, и пересоздание плеера.
    now: nowPlaying(),
    players: playerStates(),
  });

  const playerStates = () =>
    Array.from(document.querySelectorAll("video, audio")).map((item) => ({
      src: item.currentSrc || item.src || "",
      quiet: Boolean(item.paused || item.ended || item.muted),
      // Округляем: у MediaSource длительность уточняется по ходу загрузки, и
      // дробные доли секунды меняются сами по себе.
      duration: Math.round(item.duration || 0),
      time: item.currentTime || 0,
    }));

  /** Пошло ли что-то новое по сравнению со слепком. */
  const restarted = (before) => {
    if (!sounding()) {
      return false;
    }
    if (!before || !Array.isArray(before.players)) {
      return true;
    }
    // Название сменилось — сайт сам сказал, что пошёл другой трек. Этому
    // признаку верим сразу: он не зависит от того, нашли ли мы плеер.
    const now = snapshot();
    if (now.now.title && now.now.title !== before.now.title) {
      return true;
    }
    if (now.players.length !== before.players.length) {
      return true;
    }
    return now.players.some((item, index) => {
      const was = before.players[index];
      if (!was) {
        return true;
      }
      // Время назад — трек начался с начала. Полсекунды запаса: обычное
      // воспроизведение время только увеличивает.
      return (
        item.src !== was.src ||
        item.quiet !== was.quiet ||
        item.duration !== was.duration ||
        item.time + 0.5 < was.time
      );
    });
  };

  /**
   * Дождаться звука.
   *
   * Плеер запускается не мгновенно: сайт успевает сходить за ссылкой на файл.
   * Проверять сразу после нажатия бессмысленно — тишина ничего не значит. Но и
   * ждать долго нельзя: расширение держит ответ, а у команды есть свой предел.
   */
  const awaitSound = async (limit, before = null) => {
    const started = () => (before === null ? sounding() : restarted(before));
    for (let waited = 0; waited < limit; waited += 100) {
      if (started()) {
        return true;
      }
      await new Promise((done) => setTimeout(done, 100));
    }
    return started();
  };

  /** Чем закончилось дело с точки зрения звука — строкой для лога.

    Отчёт не имеет права упасть: его зовут как раз тогда, когда уже что-то не
    сработало, и уронить им ответ значило бы потерять и саму причину.
  */
  const heardNow = (before) => {
    try {
      return describeSound(before);
    } catch (error) {
      return `состояние звука не прочиталось: ${(error && error.message) || error}`;
    }
  };

  const describeSound = (before) => {
    const now = snapshot();
    const track = now.now.title || "название не сообщается";
    const was = before && before.now ? before.now.title : "";
    return (
      `плееров ${now.players.length}, состояние «${now.now.state || "не сообщается"}», ` +
      `трек «${track}»${was && was !== track ? ` (было «${was}»)` : ""}`
    );
  };

  /** Подписи, которыми называют остановку. Кнопка с такой — уже играет. */
  const PAUSE_WORDS = ["пауза", "приостановить", "остановить", "pause", "stop"];

  /**
   * Кнопка воспроизведения превратилась в кнопку паузы.
   *
   * Это ответ самого сайта: «принял, играю». Ждать тут больше нечего, даже если
   * звука ещё нет — трек может грузиться. Без этого признака оставался зазор:
   * Яндекс Музыка сперва идёт за ссылкой на файл, и `paused` переключается не
   * сразу. Не дождавшись, Jarvis шёл нажимать всё подряд — а трек уже играл.
   */
  const saysPlaying = (button) =>
    Boolean(button) && PAUSE_WORDS.some((word) => hits(spaced(caption(button)), word));

  /** Дождаться, что пошёл **новый** трек: либо сменился звук, либо кнопка стала паузой. */
  const awaitStart = async (button, limit, before) => {
    const started = () => restarted(before) || saysPlaying(button);
    for (let waited = 0; waited < limit; waited += 100) {
      if (started()) {
        return true;
      }
      await new Promise((done) => setTimeout(done, 100));
    }
    return started();
  };

  /**
   * Что было рядом: подписи и признаки. Это данные для лога, по которым потом
   * пишется рецепт сайта.
   *
   * Из классов берётся один, и не первый попавшийся: сборка склеивает их
   * десятками (`uwk3hf… IlG7b1… HbaqudSq… undefined …`), и такой «селектор»
   * бесполезен. Класс с двойным подчёркиванием — это имя из CSS-модуля
   * (`PlayButtonWithPosition_playButton__7cfDQ`), единственное осмысленное.
   */
  const listOf = (elements) =>
    elements.slice(0, 8).map((item) => {
      const classes = String(item.getAttribute("class") || "").split(/\s+/);
      const telling = classes.find((name) => name.includes("__")) || classes[0] || "";
      return {
        name: caption(item).slice(0, 40),
        sel: item.getAttribute("data-test-id") || telling,
      };
    });

  /**
   * Включить названное: трек в списке, ролик в подборке.
   *
   * Нажатие по строке трека воспроизведение не запускает — оно её только
   * выбирает или уводит на страницу трека. Играть начинает отдельная кнопка,
   * и она обычно появляется лишь при наведении мыши. Поэтому: находим строку
   * по названию, наводимся на неё, ищем внутри кнопку воспроизведения, а если
   * её нет — нажимаем саму строку и дожимаем плеер.
   *
   * **Нажатие — не то же самое, что звук.** Живой случай: на Яндекс Музыке
   * строка нашлась, кнопка «Воспроизведение» нажалась, Jarvis отчитался
   * «включаю» — и тишина. Поэтому с `play: true` успехом считается только
   * появившийся звук: не появился — пробуем следующий способ, а не врём.
   */
  const playItem = async (step) => {
    const ITEMS =
      'a[href], button, [role="button"], [role="row"], [role="listitem"], li, tr';
    const wanted = (step.item || []).map((word) => spaced(word).trim()).filter(Boolean);
    const hints = (step.hint || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!wanted.length) {
      return null;
    }

    // Верхний результат выдачи, если он назван, важнее сравнения названий:
    // порядок расставил сайт, и знает он о треках больше, чем мы — об
    // услышанном названии. Приходит это только с той выдачи, которую Jarvis
    // открыл сам; на чужой странице `prefer` не присылают.
    //
    // Видимость намеренно не проверяется: кнопка на большой карточке появляется
    // при наведении, и с проверкой её не найти никогда — та же причина, что и у
    // кнопки внутри строки ниже.
    const preferred = (selectors) => {
      for (const selector of selectors) {
        try {
          const node = document.querySelector(String(selector));
          if (node) {
            return node;
          }
        } catch (error) {
          continue; // селектор мог устареть вместе с редизайном
        }
      }
      return null;
    };
    const top = Array.isArray(step.prefer) ? preferred(step.prefer) : null;

    // Иначе побеждает самое тесное совпадение. У списка треков подпись
    // начинается с первого трека, и без этого выбирался весь список целиком:
    // «i need your love oliver nelson & tobtok remix… midnight city…».
    const found = top || bestMatch(ITEMS, wanted, [], true);
    if (!found) {
      // Ничего не совпало. Расскажем, что было **ближе всего** к просьбе, а не
      // что попалось первым: первыми на странице стоят пункты бокового меню
      // («главная; поиск; моя волна…»), и такой список не говорил ни о чём.
      // По ближайшим сразу видно, дело в услышанном названии или выдача ещё не
      // дорисовалась и сравнивать было не с чем.
      // Мера тут своя, без порога: для отчёта важно даже одно общее слово, а
      // для нажатия его мало. С порогом список опять уехал бы в меню — у всех
      // пунктов ноль, а «поиск» ещё и короче остальных.
      const nearness = (label) =>
        Math.max(
          ...wanted.map((word) => {
            if (hits(label, word)) {
              return 1;
            }
            const words = keywords(word);
            return words.length
              ? words.filter((one) => hits(label, one)).length / words.length
              : 0;
          }),
        );

      const near = [];
      let counted = 0;
      for (const element of document.querySelectorAll(ITEMS)) {
        const text = seen(element) ? caption(element) : "";
        if (!text) {
          continue;
        }
        counted += 1;
        if (text.length <= 60 && !near.some((item) => item.text === text)) {
          near.push({ text, rank: nearness(spaced(text)) });
        }
      }
      near.sort((first, second) => second.rank - first.rank || first.text.length - second.text.length);
      // `counted` важнее списка. Пустая страница и полная, но без нужного, —
      // это разные болезни: первая лечится ожиданием, вторая названием. По
      // одному списку они выглядели одинаково, потому что пустой список в лог
      // просто не попадал.
      return { saw: near.slice(0, 8).map((item) => item.text), counted };
    }

    // Кнопка воспроизведения прячется до наведения, поэтому видимость у неё
    // не проверяем — иначе её не найти никогда. И часто у неё вообще нет
    // подписи: внутри только иконка. Тогда остаётся признак в атрибутах.
    const MARKS = '[data-test-id*="PLAY" i], [class*="play" i], [aria-label*="play" i]';
    // Что играло **до** нашего вмешательства. Сравнивать будем с этим.
    const before = snapshot();
    let node = found;
    let nearby = [];
    let pressed = "";
    let control = null;
    for (let depth = 0; node && depth < 6; depth += 1) {
      for (const name of ["pointerover", "mouseover", "mouseenter"]) {
        try {
          node.dispatchEvent(new MouseEvent(name, { bubbles: true }));
        } catch (error) {
          // Событие не прошло — не страшно, кнопка может быть и так видна.
        }
      }
      const inside = Array.from(node.querySelectorAll(CLICKABLE));
      if (inside.length) {
        nearby = inside;
      }
      let button = inside.find((item) =>
        hints.some((word) => hits(spaced(caption(item)), word)),
      );
      if (!button) {
        try {
          const mark = node.querySelector(MARKS);
          // Признак «похоже на play» сидит и на иконке **внутри** кнопки.
          // Нажимать надо кнопку: поднимаемся от находки к ближайшему нажимаемому.
          button = mark && mark.closest ? mark.closest(CLICKABLE) || mark : mark;
        } catch (error) {
          button = null;
        }
      }
      if (button) {
        control = button;
        // Кнопка уже показывает паузу — значит этот трек играет прямо сейчас.
        // Нажать её означало бы остановить музыку в ответ на «включи».
        if (step.play && saysPlaying(button)) {
          return { detail: `${caption(found)} — уже играет`, played: true };
        }
        press(button);
        pressed = caption(button) || "кнопка без подписи";
        break;
      }
      node = node.parentElement;
    }

    // У кнопки на большой карточке своей подписи может не быть вовсе, а
    // «воспроизведение — воспроизведение» в логе не говорит ни о чём.
    const name = top ? `верхний результат${caption(found) ? `: ${caption(found)}` : ""}` : caption(found);
    const detail = pressed ? `${name} — ${pressed}` : name;

    // Просили просто нажать — на этом всё, звук тут никто не обещал.
    if (!step.play) {
      if (pressed) {
        return { detail };
      }
      press(found);
      return { detail, buttons: listOf(nearby) };
    }

    // Ждём не загрузку трека, а ответ сайта: либо пошёл звук, либо кнопка
    // превратилась в паузу. Второе не менее надёжно — сайт сам говорит, что
    // принял нажатие, — и закрывает зазор, в котором Яндекс Музыка ещё идёт за
    // ссылкой на файл. Пределы короткие: расширение держит ответ, а с
    // секундными паузами на каждый шаг одна команда занимала 18 с и упиралась
    // в «расширение не ответило».
    if (pressed && (await awaitStart(control, 1200, before))) {
      return { detail, played: true };
    }

    // Кнопка не помогла или её не было — нажимаем саму строку. Нажатие могло
    // только выбрать трек, поэтому потом дожимаем плеер руками.
    press(found);
    if (await awaitStart(control, 700, before)) {
      return { detail, played: true };
    }
    const target = mainPlayer();
    if (target) {
      try {
        await target.play();
      } catch (error) {
        // Автозапуск запрещён — но строку мы всё-таки нажали.
      }
    }
    const played = await awaitSound(400, before);
    // Звука так и нет — расскажем, что было рядом. По этому списку в логе
    // пишется точный рецепт сайта, без угадывания. Плюс **что мы вообще
    // слышали**: сколько плееров нашлось в разметке и что сайт сообщил о
    // текущем треке. Без этих двух чисел «не заиграло» неотличимо от «заиграло,
    // но мы не увидели» — а разница между ними и есть вся разница.
    return played
      ? { detail, played }
      : { detail, played, buttons: listOf(nearby), heard: heardNow(before) };
  };

  /** Первый видимый элемент по списку селекторов — вместе с самим селектором. */
  const bySelector = (selectors) => {
    for (const selector of selectors) {
      let found = null;
      try {
        found = document.querySelector(String(selector));
      } catch (error) {
        continue; // селектор из памяти мог устареть или оказаться кривым
      }
      if (found && seen(found)) {
        return { element: found, selector: String(selector) };
      }
    }
    return null;
  };

  // `sel` возвращается, чтобы нажатое можно было потом назвать по имени: если
  // нажалось не то, Jarvis запомнит это как «сюда больше не надо».
  const answer = (done, detail, sel = "") => ({
    done,
    detail,
    sel,
    title: document.title,
    url: location.href,
  });

  //: Что видно на странице, если название не нашлось. Уходит в лог Jarvis.
  let missed = null;
  //: На чём шаги спотыкались. Пусто — значит просто не нашлось.
  const broke = [];

  for (const step of (plan || []).slice(0, MAX_STEPS)) {
    if (!step || typeof step !== "object") {
      continue;
    }
    try {
      if (typeof step.media === "string") {
        const detail = await media(step.media, step);
        if (detail) {
          return answer("media", detail);
        }
      } else if (Array.isArray(step.suggest)) {
        const corrected = suggestion(step);
        if (corrected) {
          return answer("suggest", corrected);
        }
      } else if (typeof step.scroll === "string") {
        const where = scrollPage(step);
        if (where) {
          return answer("scroll", where);
        }
      } else if (Array.isArray(step.range)) {
        const moved = slide(step);
        if (moved) {
          return answer("range", moved.detail, moved.selector);
        }
      } else if (typeof step.type === "string") {
        const typed = typeInto(step);
        if (typed) {
          return answer("type", typed);
        }
      } else if (Array.isArray(step.item)) {
        const done = await playItem(step);
        if (done && done.detail !== undefined) {
          const reply = answer("item", done.detail);
          if (done.buttons && done.buttons.length) {
            reply.buttons = done.buttons;
          }
          if (done.played !== undefined) {
            // Нажали, но звука нет: пусть Jarvis скажет об этом честно, а не
            // отчитается «включаю» в тишину.
            reply.played = done.played;
          }
          if (done.heard) {
            // Что при этом слышала сама страница — в лог. Без этого «не
            // заиграло» неотличимо от «заиграло, но мы не увидели».
            reply.heard = done.heard;
          }
          return reply;
        }
        if (done && done.saw !== undefined) {
          missed = { saw: done.saw, counted: done.counted || 0 };
        }
      } else if (Array.isArray(step.label)) {
        const element = byLabel(step);
        if (element) {
          press(element);
          return answer("label", caption(element));
        }
      } else if (Array.isArray(step.click)) {
        const found = bySelector(step.click);
        if (found) {
          press(found.element);
          return answer(
            "click",
            found.element.getAttribute("aria-label") || caption(found.element),
            found.selector,
          );
        }
      }
    } catch (error) {
      // Один вариант не вышел — это ожидаемо, пробуем следующий. Но **сказать
      // об этом обязательно**: молчаливый обрыв уже стоил нескольких заходов
      // разбора. Ошибка внутри страницы выглядела ровно как «ничего не
      // нашлось», и объяснить её было нечем.
      broke.push(`${Object.keys(step).join("+")}: ${(error && error.message) || error}`);
    }
  }

  const nothing = answer(null, "");
  if (missed) {
    nothing.saw = missed.saw;
    nothing.counted = missed.counted;
  }
  if (broke.length) {
    nothing.broke = broke;
  }
  // Не сработало вообще ничего — скажем, что у страницы есть по части звука.
  // Половина шагов (пауза, громкость, перемотка) опирается на сам плеер, и
  // «плееров 0» объясняет неудачу мгновенно: у сайта его просто не видно в
  // разметке. Без этой строки причина неотличима от «команда не подошла».
  nothing.heard = heardNow(null);
  return nothing;
}

/**
 * Перечислить кнопки страницы: подпись плюс селектор.
 *
 * Нужно ровно для одного случая — когда ни один известный вариант не сработал
 * и решение приходится спрашивать у языковой модели. Поэтому список короткий и
 * подписи урезаны: это одна платная реплика, и платить за неё второй раз для
 * того же сайта уже не придётся — выбор уходит в память.
 *
 * @param {number} limit сколько кнопок вернуть.
 * @returns {Object} {controls: [{name, sel}], title, url}
 */
function jarvisProbe(limit) {
  const MAX_NAME = 60;
  const CLICKABLE = 'button, [role="button"], [role="switch"], [role="menuitem"]';
  const STABLE = ["data-test-id", "data-testid", "data-l", "aria-label", "name"];

  const seen = (element) => {
    const box = element.getBoundingClientRect();
    if (!box.width || !box.height) {
      return false;
    }
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && style.opacity !== "0";
  };

  /** Селектор, который переживёт перезагрузку страницы. */
  const selectorFor = (element) => {
    if (element.id && /^[A-Za-z][\w-]*$/.test(element.id)) {
      return `#${element.id}`;
    }
    for (const attribute of STABLE) {
      const value = element.getAttribute(attribute);
      // Кавычка внутри значения сломала бы селектор — такие пропускаем.
      if (!value || value.includes('"') || value.length > 80) {
        continue;
      }
      const selector = `[${attribute}="${value}"]`;
      try {
        if (document.querySelectorAll(selector).length === 1) {
          return selector;
        }
      } catch (error) {
        continue;
      }
    }
    return "";
  };

  const controls = [];
  const known = new Set();
  for (const element of document.querySelectorAll(CLICKABLE)) {
    if (controls.length >= (limit || 40)) {
      break;
    }
    if (!seen(element)) {
      continue;
    }
    const name = String(
      element.getAttribute("aria-label") || element.getAttribute("title") || element.textContent || "",
    )
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, MAX_NAME);
    const selector = selectorFor(element);
    if (!name || known.has(name.toLowerCase())) {
      continue;
    }
    known.add(name.toLowerCase());
    controls.push({ name, sel: selector });
  }

  return { controls, title: document.title, url: location.href };
}
