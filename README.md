# netbox-plugins-compatibility

Проверяет, что заданный набор плагинов NetBox совместим с конкретной целевой
версией NetBox, и публикует машиночитаемый отчёт как артефакт CI.

Матрица описывается декларативно в [`compatibility.yaml`](compatibility.yaml).
Раннер поднимает NetBox нужного ref'а в отдельном venv, ставит в него плагины,
прогоняет миграции, поднимает живой инстанс и подтверждает, что плагины видны в
`/api/status/`.

## Формат конфига

```yaml
netbox:
  repository: https://github.com/netbox-community/netbox.git
  ref: v4.4.10                 # тег, ветка или коммит

plugins:
  - name: NetBox BGP           # человекочитаемое имя, идёт в отчёт
    repository: https://github.com/netbox-community/netbox-bgp.git
    ref: v0.19.0               # тег, ветка или коммит
    package: netbox_bgp        # значение для PLUGINS в configuration.py
    plugins_config:            # опционально -> PLUGINS_CONFIG[package]
      top_level_menu: false
```

| Поле | Обязательное | Смысл |
| --- | --- | --- |
| `netbox.repository`, `netbox.ref` | да | откуда и какой ref NetBox брать |
| `plugins[].name` | да | имя в отчёте |
| `plugins[].repository`, `plugins[].ref` | да | из чего собирается `pip install git+URL@ref` |
| `plugins[].package` | да | имя модуля для `PLUGINS`; это атрибут `name` в `PluginConfig` плагина, а не имя дистрибутива на PyPI |
| `plugins[].plugins_config` | нет | попадает в `PLUGINS_CONFIG[package]` |

Конфиг валидируется до запуска стадий. Ошибка структуры печатает путь до поля и
возвращает код `2`:

```console
$ python -m netbox_compat --validate-only
ERROR  invalid config: plugins[1].package: duplicate 'netbox_bgp' (also plugins[0])
```

## Локальный запуск

Нужны Python 3.12+ (для NetBox 4.6+; для 4.4 достаточно 3.10+), Postgres,
Redis, а также `libpq-dev` и компилятор — `psycopg[c]` из requirements NetBox
собирается из исходников.

```bash
pip install -r requirements.txt

python -m netbox_compat \
  --config compatibility.yaml \
  --mode isolated \
  --output-dir reports
```

Координаты бэкендов читаются из `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE` и
`REDIS_HOST/REDIS_PORT/REDIS_PASSWORD` либо задаются флагами (`--db-host`,
`--redis-port`, …). `PGDATABASE` — это существующая служебная БД, из которой
раннер создаёт и удаляет по одной БД на цель.

Полезные флаги: `--mode`, `--python`, `--startup-timeout`, `--keep-workdir`
(не удалять venv и checkout после цели), `--validate-only`.

CI вызывает ровно эту же команду — в
[`.github/workflows/compatibility.yml`](.github/workflows/compatibility.yml)
нет собственной логики.

### Режимы

| `--mode` | Цели тестирования |
| --- | --- |
| `isolated` (по умолчанию) | по одному инстансу NetBox на плагин — провал однозначно атрибутируется плагину |
| `combined` | один инстанс со всеми плагинами — ловит конфликты плагинов между собой |
| `both` | и то, и другое |

### Стадии

Стадии идут строго последовательно; провал останавливает следующие стадии
только для своей цели, остальные цели и отчёт выполняются.

| Стадия | Что делает | Чем подтверждается успех |
| --- | --- | --- |
| `install` | venv, requirements NetBox, `pip install git+URL@ref` каждого плагина | каждый `package` из конфига резолвится через `importlib.metadata` |
| `migrate` | чистая БД и Redis, генерация `configuration.py`, `manage.py migrate` | повторный `manage.py migrate --check` не находит неприменённых миграций |
| `startup` | `manage.py runserver` на 127.0.0.1 и опрос `/api/status/` | HTTP 200 и все `package` из конфига присутствуют в `plugins` ответа |

Нулевого кода возврата недостаточно ни для одной стадии.

## Поиск рабочей версии плагина (`--resolve`)

Если плагин не совместим с целевой версией NetBox, раннер может пройти его теги
от новых к старым и найти ту версию, которая **фактически** работает:

```bash
python -m netbox_compat --config compatibility.yaml --resolve --max-attempts 3
```

Перебор устроен в два этапа, и это не оптимизация, а следствие того, как
устроен NetBox:

1. **Статический отсев (секунды на весь репозиторий).** NetBox сам отвергает
   плагин, вышедший за объявленные `min_version`/`max_version`
   (`settings.py`: `warnings.warn` + `continue`), поэтому такая версия не может
   пройти `startup` в принципе. Границы читаются прямо из git-ref'а: blobless-клон
   (`--filter=blob:none --no-checkout`, ~1 с) плюс `git show <tag>:<package>/__init__.py`
   и разбор через `ast` — без установки и без импорта модуля. Теги без
   объявленных границ отсеять нельзя, они остаются кандидатами.
2. **Фактический прогон.** По кандидатам от новых к старым запускается полный
   цикл `install`/`migrate`/`startup`; первый зелёный и есть ответ. Объявленная
   совместимость сама по себе ничего не подтверждает — миграции и зависимости
   ломаются независимо от неё. Число фактических прогонов на плагин ограничено
   `--max-attempts` (по умолчанию 3).

Целевая версия NetBox для фильтра берётся из `netbox/release.yaml` на указанном
ref'е, поэтому режим работает и когда `netbox.ref` — ветка, а не тег. Если
версию определить не удалось, статический отсев отключается и теги проверяются
подряд.

Пре-релизы (`1.7-beta1`, `v4.5.0-rc2`) исключены; вернуть их — `--include-prereleases`.

Результат — секция `resolution` в `report.json`, таблица в `report.md` и файл
`compatibility.resolved.yaml` в каталоге отчёта. В `resolution.plugins[]`
полные числа лежат в `tags_eligible`/`tags_rejected`, а список
`rejected_by_declared_bounds` — это образец из первых `--max-attempts` записей,
чтобы отчёт не раздувался списком из полусотни тегов.

```yaml
plugins:
  - name: NetBox BGP
    repository: https://github.com/netbox-community/netbox-bgp.git
    ref: v0.17.0  # было v0.19.0
    package: netbox_bgp
```

`compatibility.yaml` при этом **не меняется**: перенос найденных ref'ов в
матрицу — решение человека через PR, иначе CI незаметно подменял бы предмет
проверки. Код возврата: `0` — разрешены все плагины, `1` — хотя бы один нет.

Запускается в CI отдельным workflow
[`resolve.yml`](.github/workflows/resolve.yml) по `workflow_dispatch`.

## Чтение отчёта

В `--output-dir` (по умолчанию `reports/`) появляются:

- `report.json` — машиночитаемый результат;
- `report.md` — сводка для человека, она же уходит в GitHub job summary;
- `logs/<target>/<stage>.log` — полный вывод команд стадии.

`report.json`:

```json
{
  "netbox": {"ref": "v4.4.10"},
  "status": "failed",
  "totals": {"targets": 5, "passed": 0, "failed": 5},
  "targets": [
    {
      "id": "netbox_bgp",
      "mode": "isolated",
      "status": "failed",
      "netbox_version": null,
      "python_version": null,
      "plugins": [{"package": "netbox_bgp", "ref": "v0.19.0", "installed_version": "0.19.0"}],
      "stages": [
        {"name": "install", "status": "passed", "returncode": 0, "log": "logs/netbox_bgp/install.log"},
        {"name": "migrate", "status": "failed", "returncode": 1, "detail": "manage.py migrate failed",
         "stderr_tail": ["..."], "log": "logs/netbox_bgp/migrate.log"},
        {"name": "startup", "status": "skipped", "detail": "previous stage failed"}
      ]
    }
  ]
}
```

`netbox_version` и `python_version` берутся из ответа живого инстанса, а не из
конфига, поэтому они заполняются только у целей, дошедших до `startup`.
`installed_version` — что реально встало в venv, `reported_version` — что
инстанс показал в `/api/status/`.

В отчёт попадают только последние 20 строк stdout/stderr упавшей команды;
целиком вывод лежит в файле, на который указывает `log`.

Код возврата раннера: `0` — все цели прошли, `1` — хотя бы одна упала (отчёт при
этом сформирован), `2` — конфиг невалиден.

## Добавление нового плагина

1. Возьмите фактические данные плагина, не по памяти:
   ```bash
   git ls-remote --tags --refs https://github.com/<owner>/<repo>.git   # какие ref существуют
   git clone --depth 1 --branch <ref> https://github.com/<owner>/<repo>.git /tmp/p
   grep -n "name = \|min_version\|max_version" /tmp/p/*/__init__.py    # package и границы версий
   ```
2. Добавьте блок в `plugins:` в `compatibility.yaml`. `package` — это `name` из
   `PluginConfig`, а не имя дистрибутива (`netbox_plugin_dns` против
   `netbox-plugin-dns`).
3. Проверьте структуру: `python -m netbox_compat --validate-only`.
4. Прогоните только новый плагин: временно оставьте его одного в конфиге либо
   запустите с `--mode isolated` и посмотрите его цель в отчёте.
5. Закоммитьте `compatibility.yaml` — push по этому файлу запускает workflow.

Если плагин объявляет `min_version`/`max_version`, несовместимые с
`netbox.ref`, NetBox отвергнет его при загрузке приложений и цель упадёт на
стадии `migrate` — это ожидаемый результат проверки, а не поломка раннера.
