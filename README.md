# FindClient

Первая рабочая версия сборщика публичных контактов компаний для звонков и переписки.
Python 3.10+, SQLite, без внешних Python-зависимостей. Подходит для Ubuntu VPS.

## Быстрый запуск

```bash
git clone https://github.com/IgorAIdev/FindClient.git
cd FindClient
python3 findclient.py import seeds.json
python3 findclient.py crawl --limit 7
python3 findclient.py list
python3 findclient.py export contacts.csv
python3 findclient.py report report.html
```

Открыть report.html в браузере, CSV — в Excel/LibreOffice. Это статический отчёт,
не веб-сервер. База сохраняется в data/findclient.sqlite3. Для другого пути указать
`--db /путь/база.sqlite3` перед командой.

## Поиск новых компаний

В campaign.json задаются поисковые запросы и название кампании. Используется
[Brave Search API](https://api-dashboard.search.brave.com/documentation/guides/authentication).
Ключ задаётся переменной окружения BRAVE_SEARCH_API_KEY на сервере; не записывать его
в GitHub. API может требовать оплаты по условиям выбранного плана.

```bash
python3 findclient.py search campaign.json
python3 findclient.py crawl --limit 20
```

По 20 результатов на запрос. Поисковая выдача содержит кандидатов, в том числе
каталоги и продавцов: она не доказывает, что компания является производителем.
До ручной проверки у всех роль unverified. Стартовые семь сайтов найдены
6 октября 2026; наличие небольшого заказа и интерес к бартеру не проверены.

## Работа с контактами

```bash
python3 findclient.py update 1 --status called --notes "Перезвонить в пятницу"
python3 findclient.py update 1 --role manufacturer
python3 findclient.py update 2 --status do_not_contact
```

Статусы: new, called, written, replied, interested, closed, do_not_contact.
Роли: manufacturer, seller, installer, directory, unverified.
Повторный импорт не стирает статус и заметки. Совпадающие домены объединяются;
в текущей версии один домен = одна компания, первый campaign сохраняется.
Источники обнаружения записываются отдельно. На площадках с множеством компаний
на одном домене потребуется отдельный адаптер — их лучше пока проверять вручную.

Каждый контакт хранит URL источника и дату извлечения. Старые контакты сохраняются
с прежней датой, даже если исчезли с сайта. Дата извлечения не означает проверку
дозвоном или доставкой письма. Производитель не определяется автоматически.

## Регулярный запуск на Ubuntu

После ручного успешного запуска можно добавить cron (пример для установки в
/opt/FindClient). Ключ хранить в отдельном приватном файле /opt/FindClient/.env
с shell-строкой `export BRAVE_SEARCH_API_KEY=...` и правами chmod 600.

```cron
0 8 * * 1-5 cd /opt/FindClient && . ./.env && /usr/bin/flock -n /tmp/findclient.lock sh -c 'python3 findclient.py search campaign.json && python3 findclient.py crawl --limit 30 && python3 findclient.py report report.html' >> data/run.log 2>&1
```

Перед включением cron создать data (`mkdir -p data`). Расписание использует часовой
пояс сервера. Не запускать сборщики одновременно с одной базой. Само расписание
этим репозиторием не устанавливается. Существующие проекты VPS не меняются.

## Ограничения первой версии

- Собирается обычный HTML и контактные страницы, максимум 4 страницы на компанию.
- robots.txt учитывается; при ошибке его загрузки сайт пропускается, кроме 404.
- Между страницами пауза от 2 секунд. Блокировки и CAPTCHA не обходятся.
- JS-сайты, скрытые контакты, OLX и соцсети требуют отдельных адаптеров.
- Телефоны в тексте могут содержать ошибки; перед звонком проверить источник.
- Нет автоматической рассылки, оценки качества сайта или поиска компаний без сайта.
- Публичный код не содержит рабочей базы, секретов и заметок.
- Для недоверенных источников запускать в изолированном процессе с запретом доступа
  к внутренней сети на уровне firewall; DNS-проверка в коде не заменяет этот запрет.

## Проверка

```bash
python3 -m unittest discover -s tests -v
```

Проверяются извлечение контактов, сохранность статусов, дедупликация, robots.txt,
экранирование отчёта и защита CSV от формул. Онлайн-поиск требует действующего ключа.
