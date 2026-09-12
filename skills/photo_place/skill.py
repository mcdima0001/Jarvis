"""Где снято: место по фотографии — на экране или в файле.

Скилл появился из живого разбора 12.09.2026 и переделывался пять раз. Каждая
переделка — отдельное возражение владельца или отдельный замер, и порядок их
важен.

**«EXIF не всегда есть».** Первая версия читала координаты из файла. Проверка
подтвердила возражение буквально: из 874 снимков на машине владельца GPS не
оказалось **ни у одного**. Мессенджеры вырезают его при отправке, у скриншота
его нет по построению, а именно их ассистенту и показывают. EXIF остался, но
как удача, а не опора: когда он есть, он точен.

**«Город я и сам найду».** Вторая версия просила у модели до трёх версий, каждую
точкой, и выбирала ту, что нашлась на карте точнее. Живой прогон показал, что
это худшее из возможных решений: по фотографии дороги под Анталией модель выдала
«перекрёсток D400 и улицы 2500. Sk» с координатами, промахнулась на двенадцать
километров — и **не назвала город**, хотя сама же прочитала на вывеске «ANTALYA
BÜYÜKŞEHİR BELEDİYESİ». Требование «дай точку» не делает модель точнее, оно
заставляет её сочинять.

**Отсюда лестница.** Модель заполняет ступени от страны к месту и **на каждой
имеет право написать «нет»**. Берётся первая, которую знает геокодер: порядок
ступеней — это порядок доверия самой модели. Лестница честна, но потолок у неё
город, и владельцу этого мало.

**«Хотя бы 500 метров».** Пятая версия, и она про другое. Догадка модели о месте
— это **мнение**, и день замеров показал, чего оно стоит. А надпись на снимке —
**списанный факт**: вывеска либо есть, либо нет. Искать надпись по всему миру
бесполезно, а внутри уже найденного города — попадает в десятки метров.

Поэтому модель отдельно выписывает всё читаемое, и найденное ищется в OSM через
Overpass — **одним запросом на все надписи разом**. Дальше найденное собирается
в места, и решает **согласие**: две разные надписи с одного снимка, сошедшиеся в
трёхстах метрах, случайностью быть перестают. Замер 13.09.2026 на лондонском
снимке: паб «The Gatehouse» и театр «Upstairs at the Gatehouse» сошлись в
четырёх метрах, до настоящей точки съёмки сорок три метра.

Согласие обставлено тремя условиями, и каждое куплено ошибкой:

* **Считаются надписи, а не найденные имена.** Обрывок «1-й КУТУЗ» на московском
  снимке откликнулся на станцию, поликлинику, бильярдный клуб и автосалон —
  шесть имён от одной надписи, и по именам это выглядело бы шестикратным
  подтверждением.
* **Среди сошедшихся обязано быть название** — вывеска, остановка, табличка
  улицы, а не случайный текст. Слова «POLITIE» и «Amsterdam-Amstelland» с
  полицейского объявления тоже сошлись на карте, в четырёх километрах от места.
* **Надпись, рассыпанная по городу, — свидетель, но не улика.** «GATEHOUSE» в
  Лондоне нашлось в двух десятках мест. Выбрасывать такую нельзя (без неё верное
  место осталось бы без подтверждения), но и вести ею поиск не годится.

**Одинокая надпись сама по себе не ответ.** Сошлось только одно имя — выбор
делает спутник, и вопрос ему задаётся **сравнительный**: не «похоже ли это
место», а «которое из них». Разница измерена: на «похоже ли» железнодорожный
мост отвечает «да» в любом городе, и так подтвердилось место за шестьсот
километров от верного.

**Точность измеряется, а не обещается**, и говорится вслух: «с точностью до
здания», «до квартала», «только до города». Мера у каждого пути своя: у
согласия — разброс сошедшихся надписей, у протяжённого объекта — его рамка, у
точечного — ранг геокодера. Улица при этом никогда не обещает дом, как бы мала
ни была её рамка: геокодер отдаёт не всю улицу, а тот кусок, который счёл
подходящим, и по 68-метровому куску набережной скилл однажды пообещал точность
до здания с промахом в два километра.

**Догадку и замер не путаем.** Координаты из файла — «снято здесь», узнавание по
виду — «похоже на», сошедшиеся надписи — «сошлись две надписи», сверенная
версия — «сверил со спутником, сходится».

Мерить всё это есть чем: `tools/photo_bench/` собирает набор снимков с
известными координатами камеры и считает промах в метрах.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.errors import LLMNotConfigured, LLMOutOfCredits
from jarvis.core.llm import Message
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Профиль зрячей модели. Тот же, что у скилла `screen`: модель обязана уметь
#: картинки, а разбор команд идёт на самой дешёвой.
VISION_TASK = "vision"

#: Геокодер OpenStreetMap: без ключа, по названию отдаёт точку и рамку объекта.
NOMINATIM = "https://nominatim.openstreetmap.org"

#: Представляться геокодеру обязательно: их правила требуют узнаваемого имени,
#: анонимные запросы блокируют.
USER_AGENT = "Jarvis voice assistant (github.com/mcdima0001/Jarvis)"

#: Сколько ждать геокодер. Ответ нужен внутри голосовой команды.
GEOCODE_TIMEOUT = 8.0

#: Пауза между запросами к геокодеру, секунд. Их правило — не чаще раза в
#: секунду, и за нарушение закрывают доступ целиком.
PAUSE = 1.1

#: Сколько версий проверять. Каждая стоит запроса к чужому сервису, а правило
#: Nominatim — не чаще раза в секунду, то есть версии ещё и растягивают ответ.
MAX_CANDIDATES = 5

#: Запас за границей найденной области, метров. Снимок с окраины города вполне
#: сделан за его чертой, и отбрасывать такую версию было бы неверно.
MARGIN = 3_000.0

#: Длинная сторона картинки для модели. Тот же предел, что у зрения.
LIMIT = 1920

#: Что читаем как фотографию. EXIF бывает только в части форматов, но зрение
#: работает с любым, поэтому список шире.
PICTURES = frozenset({".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp"})

_ASK = {
    "ru": """Определи, где снята эта фотография. Отвечай как человек, который ищет
место всерьёз, а не с первого взгляда.

Сначала перечисли зацепки: язык и текст на вывесках, стиль архитектуры, рельеф и
силуэт гор, растительность, дорожная разметка и знаки, номера машин, тип столбов
и ограждений, положение солнца.

Часть зацепок называет место прямо, а не намёком: код на автомобильном номере,
вывеска местного органа власти, телефонный код, название на дорожном указателе.
Если такая зацепка есть — выведи из неё область и город, это не догадка.

**Отдельно выпиши всё, что на снимке написано.** Названия магазинов, кафе и
фирм, таблички с улицами, номера домов, названия остановок и станций — это то,
что потом ищется на карте и даёт точку. Читай внимательно, включая мелкое и
частично закрытое, и пиши **ровно то, что видно**: теми же буквами, без
перевода и без догадок.

Потом ответь по ступеням. Заполняй только те, в которых **уверен**; на
остальных пиши слово нет.

ЗАЦЕПКИ: <через запятую>
ТЕКСТЫ: <до восьми разных надписей со снимка через точку с запятой; повторы не
        нужны, одну и ту же надпись пиши один раз; нечего читать — нет>
УЛИЦА: <название улицы с таблички или указателя, иначе нет>
ДОМ: <номер дома с таблички, иначе нет>
ОСТАНОВКА: <название остановки, станции, платформы, иначе нет>
ЗАВЕДЕНИЕ: <название магазина, кафе, отеля, фирмы с вывески, иначе нет>
СТРАНА: <страна или нет>
ГОРОД: <город или нет>
РАЙОН: <район, посёлок или нет>
МЕСТО: <конкретное узнаваемое место: здание, отель, пляж, достопримечательность
        — ТОЛЬКО если правда его узнаёшь, иначе нет. Назови его так, как оно
        подписано на карте: коротким общеизвестным названием, а не полным
        официальным. «Дмитровский кремль», а не «Успенский собор Дмитровского
        кремля»: длинное официальное название карта чаще всего не знает>
МЕСТНОЕ: <название ступени МЕСТО на местном языке или по-английски, иначе нет>

**Выдуманная улица или перекрёсток хуже честного города.** Не называй адрес,
номер дороги или пересечение улиц, если не узнаёшь место по виду: точный на вид
ответ, взятый наугад, вреднее общего, но верного. Списанное с таблички — не
догадка, его и пиши в УЛИЦА и ДОМ.""",
    "en": """Work out where this photo was taken. Answer like a person who looks
into it properly, not at first glance.

First list the clues: language and text on signs, architecture, terrain and
mountain silhouette, vegetation, road markings and signs, number plates, poles
and railings, the position of the sun.

Some clues name the place outright rather than hint at it: the code on a number
plate, a local government sign, a phone code, a name on a road sign. If you have
such a clue, derive the region and city from it — that is not guesswork.

**Separately, write out everything written in the photo.** Shop, cafe and
company names, street plates, house numbers, stop and station names — these are
what a map is then searched for, and they are what gives a point. Read
carefully, including small and partly hidden text, and write **exactly what you
see**: the same letters, no translation and no guessing.

Then answer in steps. Fill in only the ones you are **sure** about; write no on
the others.

CLUES: <comma separated>
TEXTS: <up to eight different inscriptions from the photo, separated by
        semicolons; no repeats, write each one once; nothing to read — no>
STREET: <street name from a plate or sign, otherwise no>
HOUSE: <house number from a plate, otherwise no>
STOP: <name of a stop, station or platform, otherwise no>
VENUE: <name of a shop, cafe, hotel or company from a sign, otherwise no>
COUNTRY: <country or no>
CITY: <city or no>
DISTRICT: <district, suburb or no>
PLACE: <a specific recognisable place: building, hotel, beach, landmark — ONLY
        if you truly recognise it, otherwise no. Name it the way a map labels
        it: the short common name, not the full official one. A map usually
        does not know long official titles>
LOCAL: <the PLACE name in the local language, otherwise no>

**An invented street or crossroads is worse than an honest city.** Do not give an
address, road number or street intersection unless you recognise the place by
sight: a precise-looking guess is more harmful than a general but correct one.
What you copied off a plate is not a guess — put that in STREET and HOUSE.""",
}

#: Подпись строки с зацепками. Обе раскладки: модель отвечает на языке вопроса.
_CLUES = ("зацепки:", "clues:")

#: Подписи ступеней: как их зовут по-русски и по-английски.
_FIELDS = {
    "country": ("страна:", "country:"),
    "city": ("город:", "city:"),
    "district": ("район:", "district:"),
    "place": ("место:", "place:"),
    "local": ("местное:", "local:"),
    "street": ("улица:", "street:"),
    "house": ("дом:", "house:"),
    "stop": ("остановка:", "stop:"),
    "venue": ("заведение:", "venue:"),
    "texts": ("тексты:", "texts:"),
}

#: Сколько надписей со снимка вообще рассматриваем. Предел не от жадности: на
#: японском снимке модель повторила одну и ту же вывеску сорок раз подряд и
#: упёрлась в предел ответа, так и не дойдя до ступеней.
MAX_TEXTS = 8

#: Короче этого надпись искать бессмысленно: «BAR», «OPEN», номер маршрута
#: найдутся в любом городе тысячей штук и только засорят выбор.
MIN_TEXT = 4

#: Признаки выдуманного места. Модель, которую заставляют назвать точку, не
#: отказывается — она сочиняет адрес, и звучит он убедительно. В живом прогоне
#: 12.09.2026 по фотографии дороги под Анталией она выдала «перекрёсток D400 и
#: улицы 2500. Sk» с координатами, промахнувшись на двенадцать километров, и при
#: этом **не назвала город**, хотя сама же прочитала на вывеске «ANTALYA
#: BÜYÜKŞEHİR BELEDİYESİ» и номер машины на 07.
#:
#: Поэтому такие ответы отбрасываются на ступени МЕСТО: перекрёсток, номер
#: дороги, сокращение улицы. Достопримечательность так не называют, а выдумка
#: выглядит именно так.
_FABRICATED = re.compile(
    r"перекрёст|перекрест|пересечени|intersection|junction|"
    r"улиц|sokak|sk\.|cd\.|blv|caddesi|"
    r"[deo]\s?\d{3}|шоссе|highway",
    re.IGNORECASE,
)

#: Координаты в свободном виде: «36.8969, 30.7133». Знак и дробная часть
#: необязательны, разделитель — запятая или точка с запятой.
_POINT = re.compile(r"^\s*(-?\d{1,3}(?:[.,]\d+)?)\s*[;,]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$")

#: Что срезать с краёв названия: обрамление и знаки, но не буквы.
_EDGES = " \t«»\"'`.,:;!?"

#: Чем модель отказывается от ответа целиком. Проверяется началом строки.
_REFUSALS = ("не знаю", "не могу", "непонятно", "unknown", "i cannot", "i can't", "unable")

#: Чем модель отказывается от **одной ступени**. Сравнивается целиком, а не
#: началом, и это важно: «no» началом совпало бы с Новосибирском, а «нет» — с
#: Нетанией. Пустая ступень значит «не знаю», и выдумывать за модель нечего.
_EMPTY = frozenset({"нет", "не", "no", "none", "n/a", "-", "—", "неизвестно", "unknown"})


def is_empty(value: str) -> bool:
    """Пустая ли ступень лестницы."""
    return value.strip(_EDGES).lower() in _EMPTY

#: Насколько точен найденный объект, метров по большей стороне, и как это назвать
#: вслух. Пороги из замера по геокодеру 12.09.2026: здание — 47 м, башня — 175,
#: площадь — 359, город — от двадцати километров.
PRECISION = (
    (250.0, "с точностью до здания", "within about a hundred metres"),
    (2_000.0, "с точностью до квартала", "within a couple of blocks"),
    (100_000.0, "только до города", "the city only"),
    (float("inf"), "только до региона", "the region only"),
)

#: Насколько точен объект по рангу геокодера: ранг → метры. Ранг — это
#: подробность объекта в шкале OSM, от страны (4) до дома (30).
#:
#: **Рамка объекта тут не годится, и это выяснилось замером.** У точечных
#: объектов геокодер отдаёт рамку в одиннадцать метров — всегда, чем бы объект
#: ни был: и у отеля, и у семикилометрового пляжа, и у Средиземного моря. То
#: есть по рамке метка неотличима от здания. Ранг же честен: у моря он 2, у
#: города 16, у здания 30.
RANK_METRES = (
    (30, 100.0),
    (27, 300.0),
    (24, 1_000.0),
    (20, 3_000.0),
    (16, 25_000.0),
    (12, 60_000.0),
    (0, 500_000.0),
)

#: Спутниковые тайлы для сверки. Источник открытый, просит только честно
#: представиться и не злоупотреблять.
TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"

#: Какой обзор показывать при сверке. z=16 при span=3 даёт участок около
#: полутора километров — на нём замер 12.09.2026 дал чистое разделение:
#: десять баллов верному месту и ноль трём чужим.
VERIFY_ZOOM = 16
VERIFY_SPAN = 3

#: Ниже какого балла версия считается неподтверждённой. Разделение оказалось
#: не пограничным, а полным (10 против 0), поэтому порог посередине и никакой
#: тонкой настройки не просит.
VERIFY_MIN = 5

#: Версии крупнее этого не сверяем: у города на снимке сверху нет той геометрии,
#: которую видно на фотографии, и сверка выродится в угадывание.
VERIFY_BELOW = 2_000.0

#: Сколько надписей со снимка искать на карте. Все уходят **одним** запросом к
#: Overpass, так что предел тут не про вежливость, а про длину запроса.
MAX_PINS = 6

#: Сколько совпадений брать по одной надписи у Nominatim.
PIN_MATCHES = 3

#: Прямой доступ к данным OSM: в отличие от геокодера отдаёт **все** объекты с
#: таким именем внутри рамки, а не самый «важный».
#:
#: Разница решающая, и она измерена. По вывеске «The Gatehouse» геокодер внутри
#: Лондона отдаёт три паба, и верного среди них нет: он считает его менее
#: важным. Overpass отдаёт всё — и паб на North Road в сорока метрах от места
#: съёмки, и театр «Upstairs at the Gatehouse» в двадцати, и саму North Road в
#: тридцати. Три надписи с одного снимка сошлись в одной точке, и это ответ.
#: Зеркал два, и порядок между ними выбран замером 13.09.2026, а не вкусом. На
#: одном и том же запросе по Лондону: зеркало Mail.ru — 3.9 и 4.7 с на двух
#: попытках подряд, главный сервер — 7.5 с и **429 «слишком часто»** на второй.
#: Второе зеркало тут не роскошь: в замере ровно на отказе главного потерялся
#: единственный снимок, который механизм умел разгадать.
OVERPASS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)

#: Сколько ждать Overpass. Он медленнее геокодера: запрос по всему городу
#: занимает секунды, потому что это настоящий поиск по базе, а не индекс имён.
OVERPASS_TIMEOUT = 25.0

#: Сколько объектов вообще забирать у Overpass. Предел щедрый намеренно: он
#: режет **по порядку ответа, а не по полезности**, и на московском снимке
#: обрывок «1-й КУТУЗ» своими двумя десятками заведений вытеснил бы из выдачи
#: ту самую набережную, ради которой всё и затевалось. Лишнее отсеет `winnow`.
MAX_HITS = 200

#: Насколько близко должны стоять объекты, чтобы считаться одним местом.
CLUSTER = 300.0

#: В скольких **разных местах** города надпись ещё считается уликой. Больше —
#: значит, она не про место: обрывок «1-й КУТУЗ» откликнулся на два десятка
#: заведений со словом «Кутузовский», а «GATEHOUSE» в Лондоне — на два десятка
#: сторожек и школ. Длинная улица тоже попадает сюда, и это верно: пять
#: километров проспекта места не называют.
TOO_COMMON = 5

#: Какую область вообще просматривать, метров по стороне. На рамке провинции
#: Анталья (полтораста километров) Overpass отвечает 504 и не отвечает вовсе.
#: Сорок километров закрывают любой город с окраинами.
MAX_AREA = 40_000.0

#: Точнее этого не обещаем никогда. Сойтись в одну точку надписи могут, а вот
#: снимал человек всё равно с другой стороны улицы.
FLOOR = 80.0

#: Во что оценивать попадание в одну лишь улицу, метров. Названная городская
#: улица — это от полукилометра до трёх, и точка на ней говорит «где-то тут», а
#: не «вот здесь». Полтора километра выбраны так, чтобы вслух это звучало «с
#: точностью до квартала»: «до здания» тут ложь, «только до города» — наговор
#: на себя. Длинный проспект всё равно длиннее, и это признанная слабость: у
#: геокодера не спросишь, где кончается улица.
STREET = 1_500.0

#: Служебные слова, которые на вывеске и на карте пишут по-разному или не
#: пишут вовсе. Выбрасываются из образца поиска: «UPSTAIRS AT GATEHOUSE» должно
#: находить «Upstairs at the Gatehouse».
#: Только артикли и предлоги, и это осознанно узко. Родовые слова — «Road»,
#: «набережная», «Cafe» — выбрасывать нельзя: без них «North Road» вырождается
#: в «north» и находит Нортумберленд, а с ними находит саму улицу.
_STOP = frozenset({
    "the", "and", "der", "die", "das", "und", "van", "den", "del", "della",
    "des", "les", "las", "los", "sur", "aux", "una", "uno",
})

#: Что считается улицей, а не местом. Совпадение с улицей говорит о районе, с
#: кафе — о доме, и смешивать их нельзя.
_ROADS = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "service", "living_street", "pedestrian", "track", "road",
    "footway", "path", "cycleway", "steps",
})

#: Сколько мест сверять со спутником. Сверка идёт разом, но каждая стоит
#: запроса к модели и девяти тайлов.
VERIFY_TOP = 4

#: Насколько близко должны оказаться две **разные** зацепки, чтобы считать, что
#: они говорят об одном месте. Порог из смысла: соседние дома на одной улице
#: стоят десятки метров друг от друга, а случайный тёзка в том же городе —
#: километры.
TOGETHER = 400.0

#: О чём спрашивать при сверке. Главное тут — предупредить о смене ракурса:
#: без этой оговорки модель искала на снимке СВЕРХУ горы на горизонте и на их
#: отсутствии отвечала «не совпадает» (замер 12.09.2026).
_VERIFY = {
    "ru": """Первая картинка — фотография, снятая с земли, обычным объективом.
Вторая — тот же мир, но СВЕРХУ: спутниковый снимок небольшого участка.

Ракурсы разные, и это главное. На снимке сверху по построению НЕ ВИДНО ни гор на
горизонте, ни неба, ни фасадов — их отсутствие ничего не доказывает. Сравнивать
можно только план: рисунок дорог и перекрёстков, форму крыш и расположение
построек, границу застройки и зелени, характерные объекты.

Ответь двумя строками:
СХОДСТВО: <число от 0 до 10, где 0 — ничего общего, 10 — точно это место>
ПОЧЕМУ: <одна короткая фраза>""",
    "en": """The first picture is a photo taken from the ground with an ordinary
lens. The second is the same world seen FROM ABOVE: a satellite view of a small
area.

The viewpoints differ, and that is the point. A top-down view by construction
shows no mountains on the horizon, no sky and no facades — their absence proves
nothing. Compare only the plan: roads and junctions, roof shapes and building
layout, the edge between built-up land and greenery, distinctive objects.

Answer in two lines:
MATCH: <a number from 0 to 10, where 0 is nothing in common and 10 is certainly
        this place>
WHY: <one short phrase>""",
}

#: Подпись строки с оценкой сходства.
_MATCH = ("сходство:", "match:")

#: О чём спрашивать при опознании из нескольких мест. Вопрос намеренно другой,
#: чем при сверке одного: не «похоже ли», а «которое из них». На первый вопрос
#: типовая застройка отвечает «да» где угодно, на второй — нет.
_LINEUP = {
    "ru": """Первая картинка — фотография, снятая с земли, обычным объективом.
Следующие {count} — спутниковые снимки СВЕРХУ, каждый вокруг своего места. Все
они найдены по надписям с самой фотографии, и одно из них — то самое место, где
она снята. Но может и не быть ни одного.

Ракурсы разные, и это главное. Сверху по построению НЕ ВИДНО ни гор на
горизонте, ни неба, ни фасадов — их отсутствие ничего не доказывает. Сравнивать
можно только план: рисунок дорог и перекрёстков, форму крыш и расположение
построек, границу застройки и зелени, реку, мост, площадь.

Ответь тремя строками:
СНИМОК: <номер спутникового снимка от 1 до {count}, или слово нет>
СХОДСТВО: <число от 0 до 10: насколько уверен в выборе>
ПОЧЕМУ: <одна короткая фраза>""",
    "en": """The first picture is a photo taken from the ground with an ordinary
lens. The next {count} are satellite views FROM ABOVE, each around a different
place. All of them were found from text on the photo itself, and one of them is
the place where it was taken. Or possibly none of them is.

The viewpoints differ, and that is the point. From above you by construction see
no mountains on the horizon, no sky and no facades — their absence proves
nothing. Compare only the plan: roads and junctions, roof shapes and building
layout, the edge between built-up land and greenery, a river, a bridge, a square.

Answer in three lines:
IMAGE: <the number of the satellite view from 1 to {count}, or the word no>
MATCH: <a number from 0 to 10: how sure you are of the choice>
WHY: <one short phrase>""",
}

#: Подпись строки с выбранным снимком.
_CHOICE = ("снимок:", "image:")

#: Части адреса от точной к общей — для ответа по координатам из файла.
_ADDRESS = (
    "tourism", "attraction", "building", "amenity", "road",
    "suburb", "city", "town", "village", "county", "state", "country",
)


@dataclass(frozen=True, slots=True)
class Guess:
    """Одна версия модели: как называется и где, если она сказала."""

    name: str
    local: str = ""
    point: tuple[float, float] | None = None

    @property
    def queries(self) -> tuple[str, ...]:
        """Чем спрашивать геокодер, по порядку.

        Местное написание первым, и это не вежливость: «пляж Конъяалты, Анталия»
        и «отель Rixos Downtown Antalya» по-русски не находятся вовсе, а
        по-английски и по-турецки находятся (замер 12.09.2026).
        """
        names = [name for name in (self.local, self.name) if name]
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class Reading:
    """Что модель вычитала со снимка: зацепки, надписи и ступени.

    Надписи держатся отдельно от версий намеренно. Версия — это догадка модели
    о месте, а надпись — **списанный факт**: вывеска «THE GATEHOUSE» либо есть
    на снимке, либо нет, и спорить тут не о чем. Из фактов и получается точка:
    искать название по всему миру бесполезно, а внутри известного города —
    попадает в десятки метров (замер 13.09.2026).
    """

    clues: str = ""
    guesses: tuple[Guess, ...] = field(default_factory=tuple)
    texts: tuple[str, ...] = field(default_factory=tuple)
    street: str = ""
    house: str = ""
    stop: str = ""
    venue: str = ""

    @property
    def empty(self) -> bool:
        """Нечего проверять."""
        return not self.guesses and not self.named

    @property
    def strong(self) -> tuple[str, ...]:
        """Надписи, про которые модель прямо сказала, что это **название**.

        Табличка улицы, номер дома, остановка, вывеска заведения — всё это
        названия по своей природе, и найденное по ним место есть место.

        Отличать их от прочих надписей пришлось после замера 13.09.2026. На
        снимке дорожного щита в Амстердаме читались «POLITIE» и
        «Amsterdam-Amstelland» — служебные слова с полицейского объявления. На
        карте они честно сошлись: отделение полиции и штаб округа в тридцати
        метрах друг от друга. И то и другое существует, вот только сняли щит в
        четырёх километрах оттуда.
        """
        found: list[str] = []
        if self.street and self.house:
            found.append(f"{self.street} {self.house}")
        for name in (self.stop, self.venue, self.street):
            if name:
                found.append(name)
        return tuple(dict.fromkeys(found))

    @property
    def named(self) -> tuple[str, ...]:
        """Надписи, которые стоит искать на карте, от точной к общей.

        Порядок — это порядок надёжности. Адрес с табличкой дома точнее всего;
        остановка и заведение стоят на одном месте и попадают в здание;
        остальные надписи идут последними, потому что среди них и реклама, и
        объявления, и слоганы.
        """
        found = [*self.strong]
        for text in self.texts:
            if len(text) >= MIN_TEXT:
                found.append(text)
        return tuple(dict.fromkeys(found))[:MAX_TEXTS]


@dataclass(frozen=True, slots=True)
class Hit:
    """Объект на карте, найденный по надписи со снимка."""

    name: str
    point: tuple[float, float]
    #: Что это за объект: `pub`, `bus_stop`, `secondary`. Пусто — без разбору.
    kind: str = ""
    #: Какие надписи со снимка на него откликнулись. Считаем именно их, а не
    #: названия объектов: две надписи с одного снимка в одном месте — это
    #: подтверждение, а один и тот же «Gatehouse» на двух табличках — нет.
    clues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def spot(self) -> bool:
        """Точечное ли это место.

        Улица тянется на километры, и совпадение с ней говорит только о районе;
        кафе, остановка и магазин стоят там, где стоят.
        """
        return self.kind not in _ROADS


@dataclass(frozen=True, slots=True)
class Spot:
    """Место, в котором сошлось несколько надписей со снимка."""

    point: tuple[float, float]
    names: tuple[str, ...]
    spread: float
    spotted: bool = True
    #: Сколько **разных надписей со снимка** сюда попало. Это и есть мера
    #: доверия: одна — совпадение имени, две — уже не случайность.
    clues: tuple[str, ...] = field(default_factory=tuple)
    #: Есть ли среди них хоть одна редкая. Место, собранное из одних только
    #: частых надписей, — это совпадение слов, а не место.
    solid: bool = True

    @property
    def metres(self) -> float:
        """Насколько точно это место, метров.

        Разброс сошедшихся объектов — честная мера: три вывески в тридцати
        метрах друг от друга дают тридцать метров, а одинокая улица — свою
        длину. Ниже `FLOOR` не опускаемся: точнее камера и не стоит.
        """
        if not self.spotted:
            return max(self.spread, STREET)
        return max(self.spread, FLOOR)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Место, которым можно ответить: название, точка и чем оно подтверждено."""

    name: str
    point: tuple[float, float]
    metres: float | None
    source: str
    #: Оценка сверки со спутником, 0..10. ``None`` — не сверяли.
    score: int | None = None
    #: Сколько независимых зацепок сошлось на этом месте.
    agreed: int = 1


def is_refusal(answer: str) -> bool:
    """Отказалась ли модель называть место.

    Отказ надо отличать от ответа: «не знаю», отправленное в геокодер, вернёт
    какую-нибудь деревню Незнаево, и догадка превратится в уверенный ответ.
    """
    low = answer.strip().lower().lstrip("«\"'").strip()
    return not low or any(low.startswith(word) for word in _REFUSALS)


def clean_place(answer: str) -> str:
    """Снять с названия обрамление и знаки.

    Одним набором и с обоих концов: по отдельности точка и кавычка спасают друг
    друга — «Анталия».» теряло точку и оставляло кавычку.
    """
    first = answer.strip().splitlines()[0] if answer.strip() else ""
    return first.strip(_EDGES)


def parse_point(text: str) -> tuple[float, float] | None:
    """Координаты из свободной строки. ``None`` — их там нет.

    Модель пишет то «36.8969, 30.7133», то «нет». Берём только то, что похоже на
    пару чисел, и проверяем, что они на Земле: перепутанные местами широта и
    долгота иначе уехали бы в океан молча.
    """
    found = _POINT.match(text.strip().strip(_EDGES))
    if found is None:
        return None
    try:
        latitude = float(found.group(1).replace(",", "."))
        longitude = float(found.group(2).replace(",", "."))
    except ValueError:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def parse_reading(answer: str) -> Reading:
    """Разобрать лестницу ответа: зацепки и ступени от общего к частному.

    **Версии строятся от частного к общему**, и это порядок доверия: если модель
    честно заполнила МЕСТО, оно и проверяется первым; не заполнила — берётся
    район, потом город, потом страна. Пустая ступень означает «не знаю», и
    выдумывать за модель нечего.

    Ступень МЕСТО дополнительно просеивается: выдуманный адрес отбрасывается
    (см. `_FABRICATED`), и тогда ответом становится город — общий, но верный.
    """
    said: dict[str, str] = {}
    clues = ""
    for line in answer.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        for mark in _CLUES:
            if low.startswith(mark):
                clues = stripped[len(mark) :].strip(_EDGES)
        for field_name, marks in _FIELDS.items():
            for mark in marks:
                if low.startswith(mark):
                    value = stripped[len(mark) :].strip(_EDGES)
                    if value and not is_empty(value) and not is_refusal(value):
                        said[field_name] = value

    place = said.get("place", "")
    if place and _FABRICATED.search(place):
        # Выдуманный адрес вреднее честного города: он звучит точно и уводит
        # за десяток километров (живой прогон 12.09.2026).
        place = ""

    country, city = said.get("country", ""), said.get("city", "")
    district = said.get("district", "")
    steps: list[Guess] = []
    if place:
        local = said.get("local", "")
        steps.append(Guess(name=place, local=_with_city(local, city)))
    if district:
        steps.append(Guess(name=_with_city(district, city) or district))
    if city:
        steps.append(Guess(name=_with_city(city, country) or city))
    elif country:
        steps.append(Guess(name=country))
    return Reading(
        clues=clues,
        guesses=tuple(steps[:MAX_CANDIDATES]),
        texts=split_texts(said.get("texts", "")),
        street=said.get("street", ""),
        house=said.get("house", ""),
        stop=said.get("stop", ""),
        venue=said.get("venue", ""),
    )


def split_texts(line: str) -> tuple[str, ...]:
    """Строку надписей — в список, без повторов и без мусора.

    Повторы снимаются не из аккуратности: на японском снимке модель выписала
    одну и ту же вывеску сорок раз подряд и упёрлась в предел ответа, так и не
    дойдя до ступеней (замер 13.09.2026).
    """
    parts = (piece.strip(_EDGES) for piece in re.split(r"[;\n]", line))
    seen: dict[str, None] = {}
    for part in parts:
        if len(part) >= MIN_TEXT and not is_empty(part):
            seen.setdefault(part, None)
    return tuple(seen)[:MAX_TEXTS]



def _coordinates(found: dict[str, Any]) -> tuple[float, float] | None:
    """Точка из ответа геокодера."""
    try:
        return float(found["lat"]), float(found["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _inside(area: dict[str, Any] | None, point: tuple[float, float]) -> bool:
    """Лежит ли точка внутри найденной области. Нет области — верим на слово.

    Запас в `MARGIN` не от неточности рамки, а от края: снимок с окраины города
    вполне сделан за его границей, и отбрасывать такую версию было бы неверно.
    """
    if area is None:
        return True
    box = area.get("boundingbox")
    try:
        south, north, west, east = (float(value) for value in box)
    except (TypeError, ValueError):
        return True
    margin = MARGIN / 111_320
    return (south - margin <= point[0] <= north + margin
            and west - margin <= point[1] <= east + margin)


def _with_city(name: str, wider: str) -> str:
    """Приписать к названию то, что шире, если его там ещё нет.

    Геокодеру «Коньяалты» без города найдётся где угодно, а «Коньяалты,
    Анталья» — там, где нужно.
    """
    if not name:
        return ""
    if not wider or wider.lower() in name.lower():
        return name
    return f"{name}, {wider}"


def span_metres(box: Any) -> float | None:
    """Размер найденного объекта по большей стороне, метров.

    Рамка приходит как «юг, север, запад, восток» в градусах. Долгота к полюсам
    сжимается, поэтому её умножаем на косинус широты — иначе объект в Норвегии
    выглядел бы вдвое шире, чем он есть.
    """
    try:
        south, north, west, east = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    middle = math.radians((south + north) / 2)
    tall = abs(north - south) * 111_320
    wide = abs(east - west) * 111_320 * math.cos(middle)
    return max(tall, wide)


def precision_of(found: dict[str, Any]) -> float | None:
    """Насколько точен ответ геокодера, метров. ``None`` — судить нечем.

    Два источника, и порядок между ними важен:

    * **Протяжённый объект** (линия или область) сам говорит о своём размере
      рамкой, и точнее этого не скажешь: у Красной площади 359 метров, у Твери
      двадцать один километр.
    * **Точечный объект** о размере не говорит ничего: рамка у него всегда
      одиннадцать метров — и у отеля, и у семикилометрового пляжа, и у
      Средиземного моря (замер 12.09.2026). Тут судим по рангу.

    Ошибка в оставшемся случае возможна и признаётся: пляж, отмеченный на карте
    точкой, получит «до здания», хотя тянется на километры. Цена мала — таких
    объектов немного, а обратная ошибка (объявить отель городом) обесценила бы
    ответ целиком.
    """
    rank = found.get("place_rank")
    box = span_metres(found.get("boundingbox"))
    if str(found.get("category") or found.get("class") or "").lower() == "highway":
        # **Улица — это не точка, какой бы маленькой ни пришла её рамка.**
        # Геокодер отдаёт не всю улицу, а тот её отрезок, который счёл
        # подходящим: «Бережковская набережная» пришла куском в 68 метров, и
        # скилл пообещал по нему точность до здания, промахнувшись на два с
        # лишним километра (замер 13.09.2026).
        return max(box or 0.0, STREET)
    if str(found.get("osm_type", "")).lower() in ("way", "relation") and box is not None:
        return box
    return metres_for_rank(rank)


def metres_for_rank(rank: Any) -> float | None:
    """Во что превращается ранг геокодера. ``None`` — ранга нет."""
    try:
        value = int(rank)
    except (TypeError, ValueError):
        return None
    for edge, metres in RANK_METRES:
        if value >= edge:
            return metres
    return None


def describe_precision(metres: float | None, language: str = "ru") -> str:
    """Как назвать вслух достигнутую точность. Пусто — точность неизвестна."""
    if metres is None:
        return ""
    for limit, russian, english in PRECISION:
        if metres <= limit:
            return english if language == "en" else russian
    return ""


def metres_between(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Расстояние между точками по большому кругу, метров."""
    radius = 6_371_000.0
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    inner = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(inner)))


def common_clues(spots: tuple[Spot, ...], most: int = TOO_COMMON) -> frozenset[str]:
    """Надписи, раскиданные по всему городу, а не указывающие на место.

    Полезная вывеска в городе редка: паб «The Gatehouse» один, «Upstairs at the
    Gatehouse» тем более. А обрывок вроде «1-й КУТУЗ» откликается на станцию,
    поликлинику, бильярдный клуб и автосалон разом — и все про разные места
    (замер 13.09.2026, московский снимок).

    **Считаются места, а не объекты**, и это не мелочь. Улица лежит в OSM
    десятком отрезков с одним именем, и по объектам «Бережковская набережная»
    выглядела бы такой же расхожей, как «Кутузовский», — хотя лежит она в одном
    месте, а «Кутузовский» рассыпан по двадцати.

    **Выбрасывать частую надпись целиком нельзя.** «GATEHOUSE» в Лондоне тоже
    нашлось два десятка раз, и без него верное место осталось бы с единственной
    надписью, то есть без подтверждения. Частая надпись негодна как **улика**,
    но годна как **свидетель**: сама места не называет, а сказанное другой
    надписью подтверждает.
    """
    counted = Counter(clue for spot in spots for clue in spot.clues)
    return frozenset(clue for clue, places in counted.items() if places > most)


def places(hits: tuple[Hit, ...], apart: float = CLUSTER) -> tuple[Spot, ...]:
    """Собрать объекты в места и расставить по убедительности.

    Два прохода, и второй нужен: расхожесть надписи видна только после того,
    как объекты разложены по местам.
    """
    spots = cluster(hits, apart=apart)
    common = common_clues(spots)
    marked = tuple(
        replace(spot, solid=any(clue not in common for clue in spot.clues))
        for spot in spots
    )
    return tuple(sorted(
        marked,
        key=lambda spot: (not spot.solid, -len(spot.clues), -len(spot.names), spot.spread),
    ))


def cluster(hits: tuple[Hit, ...], apart: float = CLUSTER) -> tuple[Spot, ...]:
    """Собрать найденные объекты в места и отсортировать по убеждительности.

    **Это и есть весь механизм точности.** Одна надпись, найденная на карте, —
    совпадение имени и ничего больше: «North Road» в Лондоне висит на полусотне
    остановок. Но когда в трёхстах метрах сходятся три **разные** надписи с
    одного снимка — паб, театр и улица, — случайностью это быть перестаёт.

    Сортировка по числу **разных надписей со снимка**, а при равенстве — по
    тесноте. Считать надо именно надписи, и это не придирка: в Лондоне по одной
    вывеске «GATEHOUSE» рядом нашлись «St Bartholomew's Gatehouse» и «St
    Bartholomew’s Gatehouse» — одно место, два написания апострофа. По числу
    имён такая пара обходила верный ответ; по числу надписей — нет.
    """
    spots: list[Spot] = []
    used: set[int] = set()
    for index, first in enumerate(hits):
        if index in used:
            continue
        near = [
            (other_index, other) for other_index, other in enumerate(hits)
            if other_index not in used
            and metres_between(first.point, other.point) <= apart
        ]
        used.update(other_index for other_index, _ in near)
        members = [item for _, item in near]
        names = tuple(dict.fromkeys(item.name for item in members))
        point = (
            sum(item.point[0] for item in members) / len(members),
            sum(item.point[1] for item in members) / len(members),
        )
        spread = max(
            (metres_between(point, item.point) for item in members), default=0.0
        )
        found = tuple(dict.fromkeys(clue for item in members for clue in item.clues))
        spots.append(Spot(
            point=point,
            names=names,
            spread=spread,
            spotted=any(item.spot for item in members),
            clues=found,
        ))
    return tuple(spots)


def overpass_query(names: tuple[str, ...], box: tuple[float, float, float, float]) -> str:
    """Запрос к Overpass: все объекты с такими именами внутри рамки.

    Имена уходят в **регулярное выражение**, поэтому всё, что в нём значимо,
    экранируется, а кавычки и обратные слеши выбрасываются вовсе: названия
    приходят из чужого текста на фотографии, и собирать из них запрос без
    оглядки — это то же самое, что подставлять их в SQL.
    """
    safe = [loose(name) for name in names]
    pattern = "|".join(item for item in safe if item)
    south, west, north, east = box
    return (
        f"[out:json][timeout:{int(OVERPASS_TIMEOUT)}];"
        f'nwr["name"~"({pattern})",i]({south:.5f},{west:.5f},{north:.5f},{east:.5f});'
        f"out center tags {MAX_HITS};"
    )


def loose(name: str) -> str:
    """Название с вывески — в терпимый образец для поиска. Пусто — искать нечего.

    **Вывеска и карта пишут одно и то же по-разному, и это не мелочь.** На
    снимке из Лондона написано «UPSTAIRS AT GATEHOUSE», а в OSM объект зовётся
    «Upstairs at the Gatehouse» — один артикль, и точное совпадение не находит
    ничего. Замер 13.09.2026: с точным образцом место не нашлось вовсе, с
    терпимым — нашлось в двадцати метрах.

    Поэтому служебные слова выбрасываются, а между значащими ставится «что
    угодно». Осталось меньше `MIN_TEXT` букв — образца нет: короткий кусок
    найдётся где угодно и только засорит выбор.
    """
    words = [
        word for word in re.split(r"\W+", clean_name(name).lower(), flags=re.UNICODE)
        if len(word) >= 3 and word not in _STOP
    ]
    if not words:
        return ""
    pattern = ".*".join(re.escape(word) for word in words)
    return pattern if len(pattern.replace(".*", "")) >= MIN_TEXT else ""


def clean_name(name: str) -> str:
    """Убрать из названия всё, чем можно сломать запрос."""
    return re.sub(r'["\\\n\r]', " ", name).strip()


def read_hits(answer: Any, names: tuple[str, ...] = ()) -> tuple[Hit, ...]:
    """Разобрать ответ Overpass в список объектов.

    Заодно отмечаем, какая надпись со снимка на объект откликнулась: Overpass
    ищет все имена одним запросом и не говорит, какое из них сработало, а
    считать их потом придётся.
    """
    probes = [
        (name, re.compile(pattern, re.IGNORECASE))
        for name, pattern in ((name, loose(name)) for name in names)
        if pattern
    ]
    found: list[Hit] = []
    elements = answer.get("elements") if isinstance(answer, dict) else None
    for element in elements or ():
        if not isinstance(element, dict):
            continue
        middle = element.get("center") or element
        tags = element.get("tags") or {}
        try:
            point = (float(middle["lat"]), float(middle["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        kind = next(
            (
                str(tags[key]) for key in
                ("amenity", "shop", "tourism", "railway", "public_transport",
                 "office", "leisure", "highway")
                if tags.get(key)
            ),
            "",
        )
        name = str(tags.get("name", ""))
        found.append(Hit(
            name=name,
            point=point,
            kind=kind,
            clues=tuple(probe for probe, rule in probes if rule.search(name)),
        ))
    return tuple(found)


def bounds(
    box: Any, around: tuple[float, float] | None = None, limit: float = MAX_AREA
) -> tuple[float, float, float, float] | None:
    """Рамка Nominatim — в четвёрку «юг, запад, север, восток», с ограничением.

    Рамка ужимается до `limit` вокруг центра, и это не оптимизация, а условие
    работоспособности: на рамке провинции Анталья (полтораста километров)
    Overpass отвечает 504 и не отвечает вовсе. Обрезанная рамка хуже полной
    только тем, что надпись с дальней окраины в неё не попадёт, — а без обрезки
    не попадёт ни одна.
    """
    try:
        south, north, west, east = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    middle = around or ((south + north) / 2, (west + east) / 2)
    half = limit / 2 / 111_320
    wide = half / max(math.cos(math.radians(middle[0])), 0.01)
    return (
        max(south, middle[0] - half),
        max(west, middle[1] - wide),
        min(north, middle[0] + half),
        min(east, middle[1] + wide),
    )


def _speech(best: Candidate, tail: str) -> dict[str, str]:
    """Что сказать вслух. Формулировка держится за то, чем место подтверждено.

    Разница не косметическая. «Похоже на», «сошлись три надписи» и «сверил со
    спутником» — это разные обещания, и владелец по ним решает, ехать туда или
    проверять ещё раз.
    """
    if best.agreed > 1:
        return {
            "ru": f"{best.name}{tail}. Сошлись {best.agreed} надписи со снимка.",
            "en": f"{best.name}{tail}. {best.agreed} signs from the photo agree.",
        }
    if best.score is not None and best.score >= VERIFY_MIN:
        return {
            "ru": f"{best.name}{tail}. Сверил со спутником, сходится.",
            "en": f"{best.name}{tail}. Checked against satellite, it matches.",
        }
    return {
        "ru": f"Похоже на {best.name}{tail}.",
        "en": f"Looks like {best.name}{tail}.",
    }


def tighter(candidate: float | None, current: float | None) -> bool:
    """Точнее ли новая версия прежней.

    Неизвестная точность хуже любой известной: выбирать вслепую нечего.
    """
    if candidate is None:
        return False
    return current is None or candidate < current


def map_url(latitude: float, longitude: float) -> str:
    """Ссылка на точку в OpenStreetMap."""
    return (
        f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
        f"&mlon={longitude:.6f}#map=17/{latitude:.6f}/{longitude:.6f}"
    )


def spoken_address(answer: dict[str, Any]) -> str:
    """Короткое название места из ответа геокодера — то, что скажут вслух."""
    address = answer.get("address")
    parts: list[str] = []
    if isinstance(address, dict):
        for key in _ADDRESS:
            value = address.get(key)
            if isinstance(value, str) and value and value not in parts:
                parts.append(value)
            if len(parts) == 3:
                break
    if parts:
        return ", ".join(parts)
    name = answer.get("display_name")
    return ", ".join(str(name).split(", ")[:3]) if name else ""


def coordinates_of(path: Path) -> tuple[float, float] | None:
    """Координаты съёмки из EXIF. ``None`` — их там нет, и это обычное дело.

    Через Pillow, а не своим разбором TIFF: Pillow и так нужен этому скиллу,
    чтобы показать картинку модели.
    """
    try:
        from PIL import ExifTags, Image

        with Image.open(path) as picture:
            gps = picture.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:  # noqa: BLE001 — битый файл не повод падать, просто нет координат
        return None
    if not gps:
        return None
    try:
        latitude = _degrees(gps[2], gps[1])
        longitude = _degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return round(latitude, 6), round(longitude, 6)


def _degrees(parts: Any, reference: Any) -> float:
    """Градусы, минуты и секунды EXIF — в одно число со знаком."""
    degrees, minutes, seconds = (float(part) for part in parts)
    value = degrees + minutes / 60 + seconds / 3600
    return -value if str(reference).strip().upper() in ("S", "W") else value


def picture_for_model(path: Path, *, limit: int = LIMIT) -> tuple[str, tuple[int, int]]:
    """Картинка из файла в виде ``data:``-URI для зрячей модели.

    Уменьшаем только то, что больше предела: у зрения замерено, что на участке
    от 768 до 2200 пикселей цена в токенах не меняется, а читаемость от сжатия
    портится всерьёз. Читаемость тут и есть точность: место узнают по вывеске и
    по силуэту гор, и то и другое сжатие съедает первым.
    """
    from PIL import Image

    with Image.open(path) as picture:
        picture = picture.convert("RGB")
        size = picture.size
        if max(size) > limit:
            scale = limit / max(size)
            size = (max(1, int(size[0] * scale)), max(1, int(size[1] * scale)))
            picture = picture.resize(size, Image.LANCZOS)
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=92)
    body = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{body}", size


def tile_of(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    """Номер тайла для точки в обычной схеме карт (Web Mercator)."""
    count = 2 ** zoom
    x = (longitude + 180.0) / 360.0 * count
    y = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * count
    return x, y


def stitch(tiles: dict[tuple[int, int], bytes], span: int) -> bytes:
    """Склеить сетку тайлов в одну картинку и отдать её JPEG.

    Недостающий тайл оставляем серым, а не отменяем сверку: край области или
    один сбойный запрос не повод отказываться от проверки целиком.
    """
    from PIL import Image

    canvas = Image.new("RGB", (256 * span, 256 * span), (128, 128, 128))
    for (column, row), body in tiles.items():
        with Image.open(io.BytesIO(body)) as piece:
            canvas.paste(piece.convert("RGB"), (column * 256, row * 256))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def read_match(answer: str) -> int | None:
    """Оценка сходства из ответа модели. ``None`` — оценки нет."""
    for line in answer.splitlines():
        low = line.strip().lower()
        for mark in _MATCH:
            if low.startswith(mark):
                digits = re.search(r"\d+", low[len(mark) :])
                if digits:
                    return max(0, min(10, int(digits.group())))
    return None


def read_choice(answer: str, count: int) -> tuple[int | None, int | None]:
    """Выбор и уверенность из ответа при опознании.

    Возвращает «какой снимок» и «насколько уверен». Номер вне списка читается
    как отказ: модель, назвавшая шестой из четырёх, ничего не опознала.
    """
    choice: int | None = None
    for line in answer.splitlines():
        low = line.strip().lower()
        for mark in _CHOICE:
            if low.startswith(mark):
                digits = re.search(r"\d+", low[len(mark) :])
                if digits:
                    number = int(digits.group())
                    choice = number if 1 <= number <= count else None
    return choice, read_match(answer)


def resolve_photo(path: str) -> Path | None:
    """Файл фотографии по сказанному пути. ``None`` — не нашли или не картинка."""
    cleaned = path.strip().strip('"').strip("'")
    if not cleaned:
        return None
    photo = Path(cleaned).expanduser()
    if not photo.is_file() or photo.suffix.lower() not in PICTURES:
        return None
    return photo


class PhotoPlaceSkill(Skill):
    """Называет место съёмки: по виду снимка, а при удаче — по координатам."""

    meta = SkillMeta(
        name="photo_place",
        description="Где снята фотография: на экране или в файле.",
        version="0.5.0",
        spoken=("место по фото", "где снято", "photo place"),
    )

    async def on_setup(self) -> None:
        """Приготовить клиент геокодера и очередь к нему."""
        self._client: httpx.AsyncClient | None = None
        self._gate = asyncio.Lock()
        self._last_call = 0.0

    async def on_stop(self) -> None:
        """Закрыть соединения."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        """Один клиент на весь скилл."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=GEOCODE_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    @tool(
        # Предел свой, и он больше общего. Цепочка тут длинная и вся из чужих
        # служб: прочитать снимок, найти город, спросить у Overpass все надписи
        # разом, при нужде сверить со спутником. Замер 13.09.2026 на лондонском
        # снимке — 17.6 с без чтения, и главное в них Overpass. Тридцати секунд
        # хватает не всегда, а «инструмент не ответил» на просьбу найти место
        # хуже, чем подождать: ассистент всё равно скажет «секунду» через 2.5 с.
        timeout=60.0,
        phrases=[
            "где снято это фото",
            "где снята эта фотография",
            "где сделана эта фотография",
            "где снято",
            "определи место по фотографии",
            "что это за место на фото",
            "where was this photo taken",
        ],
        reversible=False,
    )
    async def photo_place(
        self, path: str = "", hint: str = "", language: str = "ru"
    ) -> ToolResult:
        """Назвать место, где снята фотография, как можно точнее.

        Пустой путь означает «то, что сейчас на экране»: чаще всего снимок
        показывают именно так — в мессенджере или в браузере.

        Помечен необратимым по той же причине, что и зрение: картинка уходит в
        чужое облако, и делать это шагом плана без спроса нельзя.

        :param path: путь к файлу; пусто — смотреть на экран.
        :param hint: что владелец знает о снимке и чего на нём не видно: «это
            Турция», «не Анталия». Сужает поиск сильнее любых зацепок.
        :param language: язык ответа.
        """
        code = "en" if str(language).startswith("en") else "ru"
        if path.strip():
            return await self._by_file(path, code, hint)
        return await self._by_screen(code, hint)

    @tool(
        # Внутри та же цепочка плюс открытие карты, поэтому и предел тот же.
        timeout=60.0,
        phrases=[
            "открой место съёмки на карте",
            "покажи на карте где снято",
            "открой это место в картах",
            "show where this was taken on the map",
        ],
        reversible=False,
    )
    async def place_on_map(
        self, path: str = "", hint: str = "", language: str = "ru"
    ) -> ToolResult:
        """Найти место съёмки и открыть его в картах.

        :param path: путь к файлу; пусто — смотреть на экран.
        :param hint: что владелец знает о снимке и чего на нём не видно.
        :param language: язык ответа.
        """
        found = await self.photo_place(path=path, hint=hint, language=language)
        if not found.ok:
            return found
        value = found.value if isinstance(found.value, dict) else {}
        url = value.get("map_url")
        if not url:
            return ToolResult.failure(
                "место названо, а точки на карте нет",
                speech={
                    "ru": "Место назвал, а на карте показать не смог.",
                    "en": "I named the place but could not put it on the map.",
                },
            )
        if not self.tools.has("browser.open_site"):
            return found
        await self.tools.invoke("browser.open_site", {"site": url})
        place = value.get("place") or ""
        return ToolResult.success(
            value,
            speech={
                "ru": f"Открыл на карте: {place}." if place else "Открыл на карте.",
                "en": f"Opened on the map: {place}." if place else "Opened on the map.",
            },
        )

    async def health(self) -> HealthStatus:
        """Здоров, пока есть Pillow и зрячая модель: геокодер — дополнение."""
        try:
            from PIL import Image  # noqa: F401  # проверяем наличие, не зовём
        except ImportError:
            return HealthStatus.degraded("нет Pillow: pip install pillow")
        if not self.context.llm.available:
            return HealthStatus.degraded("модель не настроена: нет ключа")
        try:
            self.context.llm.profiles.get(VISION_TASK)
        except LLMNotConfigured:
            return HealthStatus.degraded(
                f"нет профиля {VISION_TASK!r} в llm.profiles конфига"
            )
        return HealthStatus.healthy()

    # --- откуда берём картинку ---------------------------------------------

    async def _by_screen(self, code: str, hint: str) -> ToolResult:
        """Спросить у зрения про то, что на экране.

        Своего снимка экрана не делаем: этим занимается скилл `screen`, и
        заводить второй захват значило бы иметь два разных ответа на вопрос
        «что видно».
        """
        if not self.tools.has("screen.look"):
            return ToolResult.failure(
                "нет скилла зрения",
                speech={
                    "ru": "Не вижу экран: модуль зрения не подключён.",
                    "en": "I cannot see the screen: the vision module is missing.",
                },
            )
        looked = await self.tools.invoke(
            "screen.look", {"question": self._question(code, hint)}
        )
        if not looked.ok:
            return looked
        return await self._answer(str(looked.value or ""), code)

    async def _by_file(self, path: str, code: str, hint: str) -> ToolResult:
        """Разобрать файл: сперва координаты, если они есть, потом вид."""
        photo = resolve_photo(path)
        if photo is None:
            return ToolResult.failure(
                f"файл не найден или это не фотография: {path!r}",
                speech={
                    "ru": "Не нашёл такую фотографию.",
                    "en": "I could not find that photo.",
                },
            )
        exact = await asyncio.to_thread(coordinates_of, photo)
        if exact is not None:
            # Координаты в файле — редкая удача, зато точная.
            return await self._by_coordinates(exact, photo)

        try:
            image, _ = await asyncio.to_thread(picture_for_model, photo)
        except Exception as exc:  # noqa: BLE001 — битый файл не повод падать
            return ToolResult.failure(
                f"не удалось прочитать {photo.name}: {exc}",
                speech={
                    "ru": "Не смог открыть эту фотографию.",
                    "en": "I could not open that photo.",
                },
            )
        try:
            said = await self._ask_model(image, code, hint)
        except LLMOutOfCredits as empty:
            self.log.error("Определить место не на что: %s", empty)
            return ToolResult.failure(
                "кончились деньги на OpenRouter",
                speech={
                    "ru": "Не могу посмотреть на фотографию: кончились деньги "
                          "на OpenRouter. Пополни счёт, и я разберусь.",
                    "en": "I cannot look at the photo: the OpenRouter account is "
                          "out of credits. Top it up and I will sort it out.",
                },
            )
        if said is None:
            return ToolResult.failure(
                "зрячая модель не ответила",
                speech={
                    "ru": "Модель не ответила про эту фотографию.",
                    "en": "The model did not answer about this photo.",
                },
            )
        return await self._answer(said, code, photo=image)

    def _question(self, code: str, hint: str) -> str:
        """Что спросить у зрения, с учётом подсказки владельца."""
        asked = _ASK[code]
        clue = hint.strip()
        if not clue:
            return asked
        # Подсказка идёт первой строкой: человек знает о снимке то, чего на нём
        # не видно, и это сужает поиск сильнее любых зацепок с картинки.
        head = "Владелец подсказывает" if code == "ru" else "The owner says"
        return f"{head}: {clue}\n\n{asked}"

    async def _ask_model(self, image: str, code: str, hint: str) -> str | None:
        """Показать картинку зрячей модели и получить зацепки с версиями.

        Пустые деньги пробрасываются наружу, а не глотаются. Проверено дорого:
        ночью 13.09.2026 замер выдал шестнадцать «не узнаю» подряд, и выглядело
        это провалом механизма — а счёт на OpenRouter просто кончился.
        """
        messages = [Message.user(self._question(code, hint), images=(image,))]
        try:
            response = await self.context.llm.complete(messages, task=VISION_TASK)
        except LLMOutOfCredits:
            raise
        except Exception as exc:  # noqa: BLE001 — сеть и тариф, не наша вина
            self.log.warning("Зрячая модель не ответила: %s", exc)
            return None
        return response.text.strip()

    # --- что делаем с версиями -----------------------------------------------

    async def _answer(self, said: str, code: str, *, photo: str = "") -> ToolResult:
        """Выбрать версию по лестнице и, если есть чем, сверить её со спутником."""
        if is_refusal(said):
            return ToolResult.failure(
                "модель места не узнала",
                speech={
                    "ru": "По этой фотографии место не узнаю.",
                    "en": "I cannot tell where this was taken.",
                },
            )
        reading = parse_reading(said)
        if reading.empty:
            # Формат не соблюдён, но ответ есть, и терять его нельзя: берём
            # первую строку как единственную версию.
            reading = Reading(guesses=(Guess(name=clean_place(said)),))
        if reading.clues:
            self.log.info("Зацепки на снимке: %s", reading.clues)

        best = await self._decide(reading, photo, code)
        if best is None:
            return ToolResult.failure(
                "версии не подтвердились",
                speech={
                    "ru": "Место назвать не берусь, ничего не сходится.",
                    "en": "I would rather not guess, nothing checks out.",
                },
            )
        accuracy = describe_precision(best.metres, code)
        payload: dict[str, Any] = {
            "place": best.name,
            "source": best.source,
            "clues": reading.clues,
            "texts": list(reading.named),
            "guesses": [item.name for item in reading.guesses],
            "exact": False,
            "checked": best.score is not None and best.score >= VERIFY_MIN,
            "agreed": best.agreed,
            "accuracy_m": round(best.metres) if best.metres is not None else None,
            "latitude": best.point[0],
            "longitude": best.point[1],
            "map_url": map_url(*best.point),
        }
        self.log.info(
            "Место по снимку: %r, точка %s, точность %s м (по зацепке %r)",
            best.name,
            best.point,
            round(best.metres) if best.metres is not None else "?",
            best.source,
        )
        # Точность говорится вслух: владельцу нужна точка, и услышать «только до
        # города» ему важнее, чем услышать название города.
        tail = f", {accuracy}" if accuracy else ""
        return ToolResult.success(payload, speech=_speech(best, tail))

    async def _decide(
        self, reading: Reading, photo: str, code: str
    ) -> Candidate | None:
        """Выбрать место: сперва по надписям, и только потом по догадке.

        Порядок здесь и есть весь смысл переделки. Догадка модели о месте — это
        мнение, и день замеров показал, чего оно стоит: остановка трамвая вместо
        выставочного центра, выдуманный перекрёсток, здание муниципалитета по
        баннеру. Надпись же — **списанный факт**: вывеска либо есть на снимке,
        либо нет. Искать её по всему миру бесполезно, а внутри найденного
        города она попадает в десятки метров (замер 13.09.2026: «The Gatehouse»
        в Лондоне — 40 м, «Бережковская набережная» в Москве — 50 м).
        """
        area = await self._area(reading)
        spots = await self._spots(reading, area) if area is not None else ()
        named = set(reading.strong)
        together = next(
            (
                spot for spot in spots
                if spot.solid and len(spot.clues) > 1 and named.intersection(spot.clues)
            ),
            None,
        )
        if together is not None:
            # Несколько разных надписей, сошедшихся в одной точке, — лучшее, что
            # тут бывает: совпасть случайно они не могли, и стоит это ноль
            # запросов к модели.
            #
            # Считаются именно **надписи со снимка**, а не найденные имена, и
            # цена ошибки тут измерена: на московском снимке обрывок «1-й
            # КУТУЗ» откликнулся на станцию, поликлинику, бильярдный клуб и
            # автосалон — шесть имён от одной надписи, и по именам это
            # выглядело бы шестикратным подтверждением.
            #
            # И среди сошедшихся обязано быть **название** (`strong`), а не
            # одни только случайные надписи: слова «POLITIE» и
            # «Amsterdam-Amstelland» с полицейского объявления тоже сошлись на
            # карте — в четырёх километрах от места съёмки.
            self.log.info(
                "Надписи сошлись в одном месте: %s (разброс %.0f м)",
                ", ".join(together.clues), together.spread,
            )
            return Candidate(
                name=await self._name_of(together.point) or ", ".join(together.names),
                point=together.point,
                metres=together.metres,
                source=", ".join(together.clues),
                agreed=len(together.clues),
            )
        alone = await self._best_spot(spots, photo, code)
        if alone is not None:
            return alone
        return await self._by_ladder(reading, area, photo, code)

    async def _spots(
        self, reading: Reading, area: dict[str, Any]
    ) -> tuple[Spot, ...]:
        """Найти надписи со снимка на карте и собрать их в места."""
        names = reading.named[:MAX_PINS]
        box = bounds(area.get("boundingbox"), _coordinates(area))
        if not names or box is None:
            return ()
        hits = await self._overpass(tuple(names), box)
        if not hits:
            return ()
        spots = places(hits)
        self.log.info(
            "Надписи на карте: %d объект(ов) в %d мест(ах)", len(hits), len(spots)
        )
        return spots

    async def _best_spot(
        self, spots: tuple[Spot, ...], photo: str, code: str
    ) -> Candidate | None:
        """Единственная надпись нашлась в нескольких местах — опознать нужное.

        Сюда попадает случай, когда сойтись было нечему: на снимке одна
        читаемая вывеска. Тогда выбор делает спутник, и вопрос ему задаётся
        сравнительный — «которое из них», а не «похоже ли».
        """
        exact = [spot for spot in spots if spot.spotted and spot.solid][:VERIFY_TOP]
        if not photo or not exact:
            return None
        pins = [
            Candidate(
                name=", ".join(spot.names),
                point=spot.point,
                metres=spot.metres,
                source=spot.names[0],
            )
            for spot in exact
        ]
        best = await self._best_pin(tuple(pins), photo, code)
        if best is None or best.score is None:
            # **Одна надпись без подтверждения — это не ответ.** Совпадение
            # имени внутри города бывает случайным: по слову «POLITIE» нашлась
            # полицейская вывеска в четырёх километрах от места, и скилл
            # объявил её точкой с точностью до здания (замер 13.09.2026).
            # Не сверили — отдаём дело лестнице, она честно скажет «город».
            self.log.info("Одинокая надпись не подтвердилась — беру ступень пошире")
            return None
        return replace(best, name=await self._name_of(best.point) or best.name)

    async def _name_of(self, point: tuple[float, float]) -> str:
        """Как это место называется на карте — чтобы было что сказать вслух."""
        answer = await self._named(point)
        return spoken_address(answer) if answer else ""

    async def _overpass(
        self, names: tuple[str, ...], box: tuple[float, float, float, float]
    ) -> tuple[Hit, ...]:
        """Спросить у OSM все объекты с такими именами внутри рамки."""
        query = overpass_query(names, box)
        for host in OVERPASS:
            try:
                response = await self._http().post(
                    host, data={"data": query}, timeout=OVERPASS_TIMEOUT
                )
                response.raise_for_status()
                answer = response.json()
            except (httpx.HTTPError, ValueError) as error:
                # Занятое зеркало отвечает отказом сразу, поэтому следующее
                # пробуем тут же: ждать нечего, а второй попытки хватает.
                self.log.debug("Overpass %s не ответил: %s", host, error)
                continue
            return read_hits(answer, names)
        self.log.warning("Overpass не ответил ни на одном зеркале")
        return ()

    async def _area(self, reading: Reading) -> dict[str, Any] | None:
        """Самая широкая уверенная ступень — граница для всего остального.

        Без границы любое совпадение названия уводит куда угодно: «EXPO 2016»
        нашлось остановкой трамвая в десяти километрах, «Финляндский мост» —
        вообще в другом городе (замеры 12 и 13.09.2026).
        """
        if not reading.guesses:
            return None
        return await self._find(reading.guesses[-1])

    async def _best_pin(
        self, pins: tuple[Candidate, ...], photo: str, code: str
    ) -> Candidate | None:
        """Сверить найденные по надписям места со спутником и взять лучшее.

        Сверка тут **сравнительная, а не пороговая**, и это важнее, чем кажется.
        Порог отвечает на вопрос «похоже ли», и на нём скилл уже обжёгся:
        железнодорожный мост похож на железнодорожный мост в любом городе, и
        сверка подтвердила место за шестьсот километров от верного (замер
        13.09.2026). Выбор из нескольких спрашивает другое — «какое из них», —
        и на этот вопрос у картинок есть разные ответы.
        """
        worth = [pin for pin in pins if pin.metres is None or pin.metres <= VERIFY_BELOW]
        if not photo or not worth:
            return worth[0] if worth else None
        if len(worth) == 1:
            score = await self._verify(photo, worth[0].point, code)
            if score is not None and score < VERIFY_MIN:
                self.log.info("Единственная надпись со спутником не сошлась")
                return None
            return replace(worth[0], score=score)
        return await self._lineup(worth[:VERIFY_TOP], photo, code)

    async def _lineup(
        self, pins: list[Candidate], photo: str, code: str
    ) -> Candidate | None:
        """Показать спутниковые виды всех мест разом и спросить, которое из них.

        Одним запросом, а не четырьмя, и причина не только в деньгах. Порознь
        каждому месту задаётся вопрос «похоже ли», а на него железнодорожный
        мост отвечает «да» в любом городе — так подтвердилось место за
        шестьсот километров от верного (замер 13.09.2026). Опознание — другой
        вопрос: не «похоже ли», а «которое из них», и тут у картинок есть
        разные ответы.
        """
        views = [await self._satellite(pin.point) for pin in pins]
        ready = [(pin, view) for pin, view in zip(pins, views, strict=True) if view]
        if not ready:
            # Спутник промолчал целиком: съёмка устарела или тайлы не пришли.
            # Это не повод отвергать надписи — просто выбираем первую.
            return pins[0]
        if len(ready) == 1:
            score = await self._verify(photo, ready[0][0].point, code)
            return replace(ready[0][0], score=score) if score is None or score >= VERIFY_MIN else None
        asked = _LINEUP[code].format(count=len(ready))
        try:
            response = await self.context.llm.complete(
                [Message.user(asked, images=(photo, *(view for _, view in ready)))],
                task=VISION_TASK,
            )
        except Exception as exc:  # noqa: BLE001 — сеть и тариф, не наша вина
            self.log.warning("Опознание не состоялось: %s", exc)
            return ready[0][0]
        choice, score = read_choice(response.text, len(ready))
        self.log.info(
            "Опознание из %d: снимок %s, уверенность %s",
            len(ready), choice if choice else "ни один", score if score is not None else "?",
        )
        if choice is None or (score is not None and score < VERIFY_MIN):
            return None
        winner = ready[choice - 1][0]
        return replace(winner, score=score)

    async def _by_ladder(
        self, reading: Reading, area: dict[str, Any] | None, photo: str, code: str
    ) -> Candidate | None:
        """Старая лестница: догадка модели о месте, потом район, потом город.

        Остаётся запасным путём, и запас этот нужен: на снимке без единой
        надписи — парк, берег реки, горная дорога — искать нечего, и честный
        город лучше молчания.
        """
        for guess in reading.guesses:
            found = await self._find(guess)
            if found is None:
                continue
            point = _coordinates(found)
            if point is None or not _inside(area, point):
                self.log.debug("Версия %r нашлась вне области — отбрасываю", guess.name)
                continue
            metres = precision_of(found)
            precise = metres is not None and metres <= VERIFY_BELOW
            if photo and precise:
                score = await self._verify(photo, point, code)
                if score is not None and score < VERIFY_MIN:
                    self.log.info("Версия %r со спутником не сошлась", guess.name)
                    continue
                return Candidate(guess.name, point, metres, "версия", score)
            return Candidate(guess.name, point, metres, "версия")
        return None

    async def _by_coordinates(
        self, point: tuple[float, float], photo: Path
    ) -> ToolResult:
        """Ответ по координатам из файла — единственный случай, когда мы знаем."""
        answer = await self._named(point)
        place = spoken_address(answer) if answer else ""
        payload = {
            "place": place,
            "exact": True,
            "accuracy_m": 10,
            "latitude": point[0],
            "longitude": point[1],
            "map_url": map_url(*point),
            "file": str(photo),
        }
        self.log.info("Координаты из EXIF: %s -> %r", point, place)
        if not place:
            return ToolResult.success(
                payload,
                speech={
                    "ru": "Координаты в снимке есть, а названия места не нашёл.",
                    "en": "The photo has coordinates but I found no place name.",
                },
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": f"Снято здесь: {place}. Это из самого снимка, точно.",
                "en": f"Taken here: {place}. That is from the photo itself, exact.",
            },
        )

    # --- сверка со спутником -------------------------------------------------

    async def _satellite(self, point: tuple[float, float]) -> str | None:
        """Спутниковый вид вокруг точки одной картинкой. ``None`` — не вышло."""
        centre_x, centre_y = tile_of(*point, VERIFY_ZOOM)
        left, top = int(centre_x) - VERIFY_SPAN // 2, int(centre_y) - VERIFY_SPAN // 2
        pieces: dict[tuple[int, int], bytes] = {}
        for column in range(VERIFY_SPAN):
            for row in range(VERIFY_SPAN):
                url = TILES.format(z=VERIFY_ZOOM, x=left + column, y=top + row)
                try:
                    response = await self._http().get(url)
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    self.log.debug("Тайл %s не пришёл: %s", url, error)
                    continue
                pieces[(column, row)] = response.content
        if not pieces:
            return None
        body = await asyncio.to_thread(stitch, pieces, VERIFY_SPAN)
        return f"data:image/jpeg;base64,{base64.b64encode(body).decode('ascii')}"

    async def _verify(self, photo: str, point: tuple[float, float], code: str) -> int | None:
        """Сверить фотографию со спутниковым видом точки. ``None`` — не удалось.

        Это тот шаг, которого не хватало весь день: догадка перестаёт быть
        догадкой, когда её проверили. Замер 12.09.2026 на Дмитровском кремле дал
        чистое разделение — десять баллов верному месту и ноль трём чужим.
        """
        view = await self._satellite(point)
        if view is None:
            return None
        try:
            response = await self.context.llm.complete(
                [Message.user(_VERIFY[code], images=(photo, view))], task=VISION_TASK
            )
        except Exception as exc:  # noqa: BLE001 — сеть и тариф, не наша вина
            self.log.warning("Сверка не состоялась: %s", exc)
            return None
        score = read_match(response.text)
        self.log.info("Сверка со спутником: %s из 10", score if score is not None else "?")
        return score

    # --- геокодер ------------------------------------------------------------

    async def _find(self, guess: Guess) -> dict[str, Any] | None:
        """Название — в объект на карте. ``None`` — геокодер такого не знает."""
        for query in guess.queries:
            await self._polite()
            try:
                response = await self._http().get(
                    f"{NOMINATIM}/search",
                    params={"q": query, "format": "jsonv2", "limit": 1},
                )
                response.raise_for_status()
                found = response.json()
            except (httpx.HTTPError, ValueError) as error:
                self.log.warning("Геокодер не ответил на %r: %s", query, error)
                return None
            if isinstance(found, list) and found and isinstance(found[0], dict):
                return found[0]
        return None

    async def _polite(self) -> None:
        """Выдержать паузу перед следующим запросом к геокодеру.

        Правило Nominatim — не чаще раза в секунду, и оно не пожелание: за
        нарушение закрывают доступ целиком. Раньше запрос был один на команду и
        вопрос не стоял; теперь их до полудюжины, и очередь стала обязательной.
        """
        async with self._gate:
            waiting = PAUSE - (asyncio.get_running_loop().time() - self._last_call)
            if waiting > 0:
                await asyncio.sleep(waiting)
            self._last_call = asyncio.get_running_loop().time()

    async def _named(self, point: tuple[float, float]) -> dict[str, Any] | None:
        """Точка — в название. ``None`` — геокодер промолчал."""
        await self._polite()
        try:
            response = await self._http().get(
                f"{NOMINATIM}/reverse",
                params={
                    "lat": f"{point[0]:.6f}",
                    "lon": f"{point[1]:.6f}",
                    "format": "jsonv2",
                    "zoom": 18,
                    "accept-language": "ru,en",
                },
            )
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError) as error:
            self.log.warning("Геокодер не назвал точку %s: %s", point, error)
            return None
        return answer if isinstance(answer, dict) else None
