# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
import requests
import json
import time
import shutil
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Глобальная сессия для переиспользования соединений ---
session = requests.Session()
session.headers.update({
    'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
})


def set_custom_user_agent(user_agent: str):
    """Устанавливает кастомный User-Agent для всех последующих запросов."""
    if user_agent:
        session.headers.update({'User-Agent': user_agent})
        print(f"[HTTP] Установлен User-Agent: {user_agent}")


def _get_soup(url: str) -> BeautifulSoup | None:
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, 'html.parser')
    except requests.RequestException as e:
        print(f"[HTTP] Ошибка запроса {url}: {e}")
        return None


def _extract_data_options(soup: BeautifulSoup) -> dict | None:
    div = soup.find('div', class_='newrecord2')
    if div and div.has_attr('data-options'):
        try:
            return json.loads(div['data-options'])
        except json.JSONDecodeError:
            pass
    return None


def _detect_company_page(url: str) -> str | None:
    soup = _get_soup(url)
    if not soup: return None
    data = _extract_data_options(soup)
    if not data: return url
    if data.get('is_company_group'):
        try:
            company_id = data['step_data']['companies']['items'][0]['id']
            return f"https://dikidi.net/ru/record/{company_id}"
        except (KeyError, IndexError):
            print("[Parser] Не удалось найти ID филиала в группе")
            return None
    return url


def _parse_masters(soup: BeautifulSoup, company_id: str = None) -> list[dict]:
    masters = []
    masters_card = soup.select_one('div.card.masters')
    if not masters_card: return masters
    for m_tag in masters_card.select('a[data-id]'):
        master = {
            'id': m_tag.get('data-id', ''),
            'name': (m_tag.find('div', class_='name') or {}).get_text(strip=True),
            'specialization': (m_tag.find('div', class_='title') or {}).get_text(strip=True)
        }
        if company_id:
            master['company_id'] = str(company_id)
        masters.append(master)
    return masters


def _parse_services_from_soup(soup: BeautifulSoup) -> list[dict]:
    services = []
    for service_tag in soup.select('div.service[data-id]'):
        title_tag = service_tag.find(class_='title')
        price_tag = service_tag.find('div', class_='price')
        category_tag = service_tag.find('div', class_='type')
        if not title_tag: continue
        services.append({
            'id':
            service_tag['data-id'],
            'name':
            title_tag.get_text(strip=True),
            'price':
            " ".join(price_tag.get_text(
                strip=True).split()) if price_tag else '',
            'master_ids': [],
            'category':
            category_tag.get_text(strip=True) if category_tag else ''
        })
    return services


def scrape_dikidi_data(base_url: str):
    print(f"[Parser] Старт парсинга {base_url}")
    company_url = _detect_company_page(base_url)
    if not company_url:
        print("[Parser] Не удалось определить URL компании")
        return None, None
    print(f"[Parser] Используем URL компании: {company_url}")
    company_id = urlparse(company_url).path.strip('/').split('/')[-1]
    company_soup = _get_soup(company_url)
    if not company_soup: return None, None
    masters = _parse_masters(company_soup, company_id)
    services = _parse_services_from_soup(company_soup)
    if not masters or not services:
        options_data = _extract_data_options(company_soup)
        if options_data and isinstance(
                options_data.get('step_data', {}).get('view'), str):
            inner_html = options_data['step_data']['view']
            inner_soup = BeautifulSoup(inner_html, 'html.parser')
            if not masters: masters = _parse_masters(inner_soup, company_id)
            if not services: services = _parse_services_from_soup(inner_soup)
    show_all = company_soup.select_one('div.card.services .show-more a')
    if show_all and ('href' in show_all.attrs):
        all_services_url = urljoin(company_url, show_all['href'])
        print(f"[Parser] Загружаем полную страницу услуг: {all_services_url}")
        services_soup = _get_soup(all_services_url)
        if services_soup: services = _parse_services_from_soup(services_soup)
    if len(services) < 10 and 'inner_soup' in locals():
        show_all_inner = inner_soup.select_one(
            'div.card.services .show-more a') if 'inner_soup' in locals(
            ) else None
        if show_all_inner and ('href' in show_all_inner.attrs):
            all_services_url = urljoin(company_url, show_all_inner['href'])
            print(
                f"[Parser] Загружаем полную страницу услуг (inner): {all_services_url}"
            )
            services_soup = _get_soup(all_services_url)
            if services_soup:
                services = _parse_services_from_soup(services_soup)
    print(f"[Parser] Найдено мастеров: {len(masters)}, услуг: {len(services)}")
    if not services:
        try:
            company_id = urlparse(company_url).path.strip('/').split('/')[-1]
            api_url = "https://dikidi.net/mobile/ajax/newrecord/company_services/"
            resp = session.get(api_url,
                               params={'company': company_id},
                               timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get('error', {}).get('code') == 0:
                services = []
                for cat_data in payload.get('data', {}).get('list',
                                                            {}).values():
                    category_name = cat_data.get('category_value', '')
                    for svc in cat_data.get('services', []):
                        services.append({
                            'id':
                            str(svc['id']),
                            'name':
                            svc['name'],
                            'price':
                            f"{svc.get('price', '')} RUB"
                            if svc.get('price') else '',
                            'master_ids': [],
                            'category':
                            category_name
                        })
                print(f"[Parser] Услуги получены через API: {len(services)}")
        except Exception as e:
            print(f"[Parser] Ошибка API company_services: {e}")
    if services and masters:
        _fetch_and_assign_masters_concurrently(company_url, services, masters)
    return masters, services


def _fetch_service_masters(svc: dict, company_id: str,
                           master_id_set: set, masters: list[dict]) -> tuple[str, list[str]]:
    svc_api_url = "https://dikidi.net/ru/mobile/ajax/newrecord/service_info_masters/"
    service_id, master_ids = svc['id'], []
    try:
        r = session.get(svc_api_url,
                        params={
                            'company_id': company_id,
                            'service_id': service_id
                        },
                        timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get('error', {}).get('code') == 0:
                lst = data.get('data', {}).get('list', [])
                # Фильтруем только мастеров с нужным company_id
                allowed_ids = {m['id'] for m in masters if str(m.get('company_id')) == str(company_id)}
                master_ids = [str(item['id']) for item in lst if str(item['id']) in allowed_ids]
    except Exception as e:
        print(f"[HTTP] Ошибка привязки услуги {service_id}: {e}")
    return service_id, master_ids


def _fetch_and_assign_masters_concurrently(company_url: str,
                                           services: list[dict],
                                           masters: list[dict]):
    company_id = urlparse(company_url).path.strip('/').split('/')[-1]
    master_id_set = {m['id'] for m in masters}
    service_map = {s['id']: s for s in services}
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_service = {
            executor.submit(_fetch_service_masters, svc, company_id, master_id_set, masters):
            svc
            for svc in services
        }
        for future in as_completed(future_to_service):
            try:
                service_id, master_ids = future.result()
                if service_id in service_map:
                    service_map[service_id]['master_ids'] = master_ids
            except Exception as e:
                svc = future_to_service[future]
                print(
                    f"[Executor] Ошибка в задаче для услуги {svc['id']}: {e}")


# --- КЭШ для расписания мастеров (раз в минуту) ---
_SCHEDULE_CACHE = {}
_SCHEDULE_CACHE_TTL = 60  # 60 секунд

def get_dikidi_schedule(company_id: str,
                        service_id: str | int,
                        master_id: str | int | None = None,
                        date: str | None = None) -> dict | None:
    cache_key = (str(company_id), str(service_id), str(master_id), str(date))
    now = time.time()
    # Проверяем кэш
    if cache_key in _SCHEDULE_CACHE:
        cached_result, cached_time = _SCHEDULE_CACHE[cache_key]
        if now - cached_time < _SCHEDULE_CACHE_TTL:
            return cached_result
    api_url = "https://dikidi.net/ru/mobile/ajax/newrecord/get_datetimes/"
    params: dict[str, str | int] = {
        'company_id': company_id,
        'service_id': service_id
    }
    if master_id not in (None, '', 0, '0'): params['master_id'] = master_id
    if date: params['date'] = date
    headers = {
        'User-Agent': session.headers['User-Agent'],
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest'
    }
    try:
        resp = session.get(api_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get('error', {}).get('code') != 0:
            print(f"[Parser] API вернул ошибку: {payload['error']}")
            return None
        result = payload.get('data', {}).get('times', {})
        _SCHEDULE_CACHE[cache_key] = (result, now)
        return result
    except requests.RequestException as e:
        print(f"[HTTP] Ошибка запроса расписания: {e}")
    except json.JSONDecodeError:
        print("[Parser] Ошибка JSON в ответе расписания")
    return None


# --- Изменение: даём project_id значение по умолчанию ---
def create_dikidi_booking(project_id: str = "322422",
                          company_id: str = None,
                          service_id: str = None,
                          master_id: str = None,
                          date: str = None,
                          time: str = None,
                          phone: str = None,
                          email: str = None,
                          animal_name: str = None,
                          comment: str = None) -> dict:
    url = "https://dikidi.net/ru/ajax/newrecord/create"
    # --- ВСЕГДА получаем project_id из data-options для данного company_id ---
    try:
        referer_url = f'https://dikidi.net/ru/record/{company_id}'
        soup = _get_soup(referer_url)
        data_options = _extract_data_options(soup) if soup else None
        if data_options and 'project_id' in data_options:
            project_id = str(data_options['project_id'])
        print(f"[DEBUG] project_id из data-options: {project_id} для company_id: {company_id}")
    except Exception as e:
        print(f"[DEBUG] Ошибка получения project_id: {e}")
    # --- service_id[] должен быть списком, если это не так ---
    service_ids = service_id if isinstance(service_id, list) else [service_id] if service_id else []
    payload = {
        'project_id': project_id,
        'company_id': str(company_id),
        'service_id[]': service_ids,
        'master_id': master_id,
        'date': date,
        'time': time,
        'phone': phone or '',
        'email': email or '',
        'animal-name': animal_name or '',
        'comment': comment or '',
        'source': 'whatsapp-bot',
        'timezone': 'Europe/Moscow',
        'personal_data_agreement': 'on',
    }
    headers = {
        'User-Agent': session.headers['User-Agent'],
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://dikidi.net',
        'Referer': f'https://dikidi.net/ru/record/{company_id}',
    }
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        error = data.get('error')
        # Универсальная обработка error
        if isinstance(error, dict):
            code = error.get('code', 1)
            message = error.get('text', 'Успешно')
        else:
            code = error
            message = data.get('message', 'Ошибка API')
        return {
            'success': code == 0,
            'message': message,
            'data': data
        }
    except requests.exceptions.RequestException as e:
        return {
            'success': False,
            'message': f'Ошибка сети при создании записи: {e}',
            'data': {}
        }
    except json.JSONDecodeError:
        return {
            'success': False,
            'message': 'Ошибка при разборе ответа сервера',
            'data': {}
        }
    except Exception as e:
        return {
            'success': False,
            'message': f'Непредвиденная ошибка при создании записи: {e}',
            'data': {}
        }
