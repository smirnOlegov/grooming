# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
import os
import datetime
import re
import json
from dotenv import load_dotenv
from whatsapp_chatbot_python import GreenAPIBot, Notification
from fsm import States
from gpt import get_gpt_response
from knowledge import get_system_prompt, get_cached_data, find_relevant_services
from scraper import get_dikidi_schedule, set_custom_user_agent, create_dikidi_booking
from babel.dates import format_datetime
from selenium_booking import book_dikidi

# Загрузка учетных данных
load_dotenv(dotenv_path='secret')
ID_INSTANCE = os.getenv("ID_INSTANCE")
API_TOKEN_INSTANCE = os.getenv("API_TOKEN_INSTANCE")
DIKIDI_PROJECT_ID = "322422"  # Добавляем project_id

# Инициализация
bot = GreenAPIBot(ID_INSTANCE, API_TOKEN_INSTANCE)
user_agent = os.getenv("USER_AGENT")
if user_agent:
    set_custom_user_agent(user_agent)

# --- ИЗМЕНЕНИЕ: Убран project_id, т.к. API требует company_id
# DIKIDI_PROJECT_ID = "322422"
MAX_HISTORY_MESSAGES = 20  # Ограничение истории для GPT (10 пар вопрос-ответ)

# --- Глобальные переменные ---
BRANCHES = [{
    'id': 'alfa',
    'name': 'ALFA grooming Строгино',
    'company_id': '658503',
    'url': 'https://dikidi.net/g322422?p=2.sp-pi-po&c=658503',
    'address': 'Таллинская 9',
    'phone': '+79993440009'
}, {
    'id': 'center',
    'name': 'ALFA grooming Щукино',
    'company_id': '1343398',  # исправлено на правильный company_id
    'url': 'https://dikidi.net/g322422?p=2.sp-pi-po&c=1343398',
    'address': 'Рогова 15к1',
    'phone': '+79993440009'
}]

conversation_histories = {}
booking_data = {}

# Ключевые слова для старта пошаговой записи
STEP_KEYWORDS = ["записаться", "запись", "хочу записаться"]

def is_step_keyword(text):
    text = text.lower()
    return any(word in text for word in STEP_KEYWORDS)

REMIND_STEP = "\n\nЧтобы начать пошаговую запись, просто напишите «записаться» в любой момент."

# --- Вспомогательные функции ---


def base_service_name(name: str) -> str:
    name = name.split('|', 1)[0]
    name = re.sub(r"\s*\([^)]*\)$", "", name)
    return name.strip()


def find_best_match(query: str, candidates: list, key_func):
    if not query or not candidates:
        return None
    # Если query — dict, пробуем взять name
    if isinstance(query, dict):
        query = query.get('name', '')
    if not isinstance(query, str):
        query = str(query)
    query_words = set(re.findall(r'\w+', query.lower()))
    best_match, max_score = None, 0
    for candidate in candidates:
        candidate_name = key_func(candidate)
        # Если candidate_name — dict, берем name
        if isinstance(candidate_name, dict):
            candidate_name = candidate_name.get('name', '')
        # --- Исправление: всегда приводим к строке ---
        candidate_name = str(candidate_name)
        candidate_words = set(re.findall(r'\w+', candidate_name.lower()))
        score = len(query_words.intersection(candidate_words))
        if score > max_score:
            max_score, best_match = score, candidate
    return best_match if max_score > 0 else None


def send_message_via_greenapi(chat_id: str, text: str):
    """Явная отправка сообщения через Green API (минуя notification.answer)."""
    import requests
    instance_id = os.getenv("ID_INSTANCE")
    api_token = os.getenv("API_TOKEN_INSTANCE")
    # Удалены все print
    if not instance_id or not api_token:
        return False
    url = f"https://api.green-api.com/waInstance{instance_id}/sendMessage/{api_token}"
    payload = {
        "chatId": chat_id,
        "message": text
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


# --- ВРЕМЕННАЯ ФУНКЦИЯ ДЛЯ РУЧНОГО ТЕСТА ---
def test_greenapi_send():
    test_chat_id = "79810100476@c.us"
    test_text = "Тестовое сообщение от GreenAPI (ручная отправка)"
    ok = send_message_via_greenapi(test_chat_id, test_text)
    # Удалён print


def start_new_booking(notification: Notification):
    sender = notification.sender
    booking_data[sender] = {}
    intro = (
        "Здравствуйте! Я бот-консультант груминг-салона. Вы можете задать любой вопрос по услугам, мастерам, животным и уходу — я с радостью помогу!\n"
        "\nЕсли вы хотите записаться на услугу, просто напишите слово «записаться» — и я проведу вас по шагам записи."
    )
    send_message_via_greenapi(sender, intro)
    try:
        notification.state_manager.reset_state(sender)
    except Exception:
        pass


@bot.router.message(state=States.CHOOSE_BRANCH)
def choose_branch_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    if not text.isdigit():
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": text})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос! 😊\n{response}\nЕсли хотите продолжить запись — просто выберите номер филиала из списка ниже или задайте ещё вопрос.")
        history.append({"role": "assistant", "content": response})
        return
    try:
        branch_index = int(text) - 1
        if 0 <= branch_index < len(BRANCHES):
            branch = BRANCHES[branch_index]
            booking_data[sender]['branch'] = branch
            masters, services = get_cached_data(branch['company_id'])
            # Фильтруем мастеров по company_id филиала, если поле есть
            filtered_masters = [m for m in masters if str(m.get('company_id')) == str(branch['company_id'])] if masters and 'company_id' in masters[0] else masters
            booking_data[sender]['ALL_MASTERS'] = filtered_masters
            booking_data[sender]['ALL_SERVICES'] = services
            notification.answer("Отлично, вы выбрали филиал! 😊\nПожалуйста, уточните породу вашего питомца (например, 'мопс', 'британская кошка', 'йоркширский терьер'). Если возникнут вопросы по услугам — с радостью подскажу!")
            notification.state_manager.set_state(sender, States.CHOOSE_SERVICE)
        else:
            raise ValueError
    except (ValueError, IndexError):
        notification.answer("Пожалуйста, выберите номер филиала из списка ниже. Если что-то непонятно — смело спрашивайте!")


@bot.router.message(state=States.CHOOSE_SERVICE)
def choose_service_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    if not text or len(text.split()) < 2 and not text.isalpha():
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": text})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос! 🐾\n{response}\nЕсли хотите продолжить запись — уточните породу или выберите услугу, либо задайте ещё вопрос.")
        history.append({"role": "assistant", "content": response})
        return
    all_services = booking_data[sender].get('ALL_SERVICES', [])
    top_services = find_relevant_services(text, all_services, top_k=9)
    services_str = "\n".join([
        f"{i+1}. {s['name']} ({s.get('price','')})" for i, s in enumerate(top_services)
    ])
    notification.answer(
        f"Вот 9 самых подходящих услуг (выберите номер):\n{services_str}\nПожалуйста, выберите номер услуги. Если нужна консультация — просто напишите свой вопрос!"
    )
    booking_data[sender]['top_services'] = top_services
    notification.state_manager.set_state(sender, States.CHOOSE_SERVICE_SELECT)


@bot.router.message(state=States.CHOOSE_SERVICE_SELECT)
def choose_service_select_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    if not text.isdigit():
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": text})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос!\n{response}\nЕсли хотите продолжить запись — выберите номер услуги из списка или задайте ещё вопрос.")
        history.append({"role": "assistant", "content": response})
        return
    try:
        service_index = int(text) - 1
        top_services = booking_data[sender]['top_services']
        if 0 <= service_index < len(top_services):
            selected_service = top_services[service_index]
            booking_data[sender]['service'] = selected_service
            all_masters = booking_data[sender]['ALL_MASTERS']
            if not all_masters:
                notification.answer("[DEBUG] all_masters is empty! Проверьте заполнение мастеров для филиала.\nПопробуйте выбрать другой филиал или обратитесь к администратору.")
                return
            master_ids = selected_service.get('master_ids', [])
            branch = booking_data[sender]['branch']
            branch_company_id = str(branch['company_id'])
            # DEBUG: выводим, что фильтруем (только в консоль)
            debug_msg = f"[DEBUG] master_ids={master_ids}, branch_company_id={branch_company_id}, all_masters={[{'id': m.get('id'), 'company_id': m.get('company_id')} for m in all_masters]}"
            print(debug_msg)
            # Фильтруем мастеров по master_ids и company_id филиала
            if master_ids:
                eligible_masters = [m for m in all_masters if m['id'] in master_ids and str(m.get('company_id')) == branch_company_id]
            else:
                eligible_masters = [m for m in all_masters if str(m.get('company_id')) == branch_company_id]
            if not eligible_masters:
                notification.answer("К сожалению, для этой услуги нет доступных мастеров в выбранном филиале. Попробуйте выбрать другую услугу или напишите, если нужна помощь.")
                return
            masters_str = "\n".join([
                f"{i+1}. {m['name']} ({m.get('specialization','')})" for i, m in enumerate(eligible_masters)
            ])
            booking_data[sender]['eligible_masters'] = eligible_masters
            notification.answer(f"Почти готово! Теперь выберите мастера для услуги '{selected_service['name']}':\n{masters_str}\nЕсли не знаете, кого выбрать — напишите, и я помогу с выбором!")
            notification.state_manager.set_state(sender, States.CHOOSE_MASTER)
        else:
            raise ValueError
    except (ValueError, IndexError):
        notification.answer(
            "Пожалуйста, выберите номер услуги из списка. Если нужна консультация — просто напишите свой вопрос!")


@bot.router.message(state=States.CHOOSE_MASTER)
def choose_master_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    if not text.isdigit():
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": text})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос!\n{response}\nЕсли хотите продолжить запись — выберите номер мастера или задайте ещё вопрос.")
        history.append({"role": "assistant", "content": response})
        return
    try:
        master_index = int(text) - 1
        eligible_masters = booking_data[sender]['eligible_masters']
        if 0 <= master_index < len(eligible_masters):
            master = eligible_masters[master_index]
            booking_data[sender]['master'] = master
            selected_service = booking_data[sender]['service']
            notification.answer(
                f"Вы выбрали мастера: {master['name']} и услугу: {selected_service['name']}\nТеперь выберите удобную дату для записи. Если нужна помощь — я всегда на связи!"
            )
            _start_date_time_selection(notification)
        else:
            raise ValueError
    except (ValueError, IndexError):
        notification.answer(
            "Пожалуйста, выберите номер мастера из списка. Если не определились — напишите, и я помогу!")


def _start_date_time_selection(notification: Notification):
    sender = notification.sender
    try:
        service = booking_data[sender]['service']
        branch = booking_data[sender]['branch']
        master = booking_data[sender]['master']
        schedule = get_dikidi_schedule(branch['company_id'], service['id'], master['id'])
        booking_data[sender]['schedule_data'] = schedule
        available_dates = []
        now = datetime.datetime.now().date()
        if schedule:
            slots = schedule.get(str(master['id'])) or schedule.get(master['id']) or []
            # Собираем все даты, когда есть хотя бы один слот
            all_dates = [dt.split()[0] for dt in slots]
            # Оставляем только будущие и сегодняшние даты
            available_dates = sorted({date for date in all_dates if datetime.datetime.strptime(date, '%Y-%m-%d').date() >= now})
        if not available_dates:
            notification.answer(
                "К сожалению, у этого мастера нет свободных слотов для выбранной услуги. Пожалуйста, попробуйте выбрать другую услугу или мастера."
            )
            start_new_booking(notification)
            return
        booking_data[sender]['available_dates'] = available_dates
        dates_list = []
        for i, date_str in enumerate(available_dates):
            dt_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            formatted_date = format_datetime(dt_obj, 'd MMMM, EEEE', locale='ru_RU')
            dates_list.append(f"{i+1}. {formatted_date.capitalize()}")
        dates_str = "\n".join(dates_list)
        notification.answer(f"Выберите удобную дату:\n{dates_str}")
        notification.state_manager.set_state(sender, States.CHOOSE_DAY)
    except Exception:
        notification.answer(
            "Произошла ошибка при получении расписания. Попробуйте, пожалуйста, начать заново."
        )
        start_new_booking(notification)


@bot.router.message(state=States.CHOOSE_DAY)
def choose_day_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    if not text.isdigit():
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": text})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос!\n{response}\nЕсли хотите продолжить запись — выберите номер даты или задайте ещё вопрос.")
        history.append({"role": "assistant", "content": response})
        return
    try:
        date_index = int(text) - 1
        available_dates = booking_data[sender].get('available_dates', [])
        if 0 <= date_index < len(available_dates):
            selected_date = available_dates[date_index]
            booking_data[sender]['date'] = selected_date
            schedule = booking_data[sender].get('schedule_data', {})
            master = booking_data[sender]['master']
            master_id = str(master['id']);
            slots = schedule.get(master_id) or schedule.get(int(master_id)) or []
            available_times = [dt.split()[1] for dt in slots if dt.split()[0] == selected_date]
            if not available_times:
                notification.answer("Нет свободного времени на выбранную дату. Попробуйте выбрать другой день или напишите, если нужна помощь.")
                return
            booking_data[sender]['available_times'] = available_times
            times_str = "\n".join([f"{i+1}. {t}" for i, t in enumerate(available_times)])
            notification.answer(f"Выберите удобное время:\n{times_str}\nЕсли не нашли подходящее время — напишите, и я помогу подобрать! 😊")
            notification.state_manager.set_state(sender, States.CHOOSE_TIME)
        else:
            raise ValueError
    except (ValueError, IndexError):
        notification.answer("Пожалуйста, выберите номер даты из списка. Если нужна консультация — просто напишите свой вопрос!")


@bot.router.message(state=States.CHOOSE_TIME)
def choose_time_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    if not text.isdigit():
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": text})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос!\n{response}\nЕсли хотите продолжить запись — выберите номер времени или задайте ещё вопрос.")
        history.append({"role": "assistant", "content": response})
        return
    try:
        time_index = int(text) - 1
        available_times = booking_data[sender].get('available_times', [])
        if 0 <= time_index < len(available_times):
            selected_time = available_times[time_index]
            booking_data[sender]['time'] = selected_time
            notification.answer("Почти всё готово! Пожалуйста, отправьте ваш номер телефона или email для подтверждения записи. Если есть вопросы — с радостью отвечу!")
            notification.state_manager.set_state(sender, States.BOOKING_CONFIRM)
        else:
            raise ValueError
    except (ValueError, IndexError):
        notification.answer("Пожалуйста, выберите номер времени из списка. Если нужна консультация — просто напишите свой вопрос!")


@bot.router.message(state=States.BOOKING_CONFIRM)
def booking_confirm_handler(notification: Notification):
    sender, contact = notification.sender, notification.message_text.strip()

    if not (re.match(r"^\+?\d{10,15}$", contact) or re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", contact)):
        history = conversation_histories.setdefault(sender, [])
        history.append({"role": "user", "content": contact})
        all_masters, all_services = get_cached_data()
        system_prompt = get_system_prompt(all_masters, all_services)
        response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
        notification.answer(f"Спасибо за ваш вопрос!\n{response}\nПожалуйста, отправьте корректный номер телефона или email. Если нужна помощь — я всегда рядом!")
        history.append({"role": "assistant", "content": response})

        return
    
    data = booking_data[sender]
    phone = contact if re.match(r'^\+?\d', contact) else ''
    email = contact if '@' in contact else ''
    branch = data.get('branch', {})
    service = data.get('service', {})
    master = data.get('master', {})
    master_name = master.get('name', 'Неизвестно')
    date = data.get('date', '')
    time_ = data.get('time', '')
    url = branch.get('url', '')
    service_name = service.get('name', 'Неизвестно')
    
    print(f"[DEBUG][API] company_id={branch.get('company_id')}, service_id={service.get('id')}, master_id={master.get('id')}, date={date}, time={time_}, phone={phone}, email={email}")
    try:
        result = create_dikidi_booking(
            company_id=branch.get('company_id'),
            service_id=service.get('id'),
            master_id=master.get('id'),
            date=date,
            time=time_,
            phone=phone,
            email=email
        )
        if result.get('success'):
            notification.answer(
                f"✅ Готово! Ваша запись успешно создана через API!\n\nФилиал: {branch.get('name', 'Неизвестно')}\nУслуга: {service_name}\nМастер: {master_name}\nДата: {date} {time_}\n\nСпасибо, что выбрали нас! Если появятся вопросы — всегда рады помочь! 😊")
            notification.state_manager.reset_state(sender)
            booking_data[sender] = {}
        else:
            notification.answer(
                f"❌ Не удалось создать запись через API. {result.get('message', '')} Пожалуйста, попробуйте другое время или обратитесь к администратору. Если нужна помощь — напишите мне!")
    except Exception as e:
        print(f"[ERROR][API] {e}")
        notification.answer(
            f"❌ Произошла ошибка при попытке записи через API: {e}\nПопробуйте позже или обратитесь к администратору. Если нужна консультация — я всегда на связи!")


@bot.router.message()
def universal_handler(notification: Notification):
    sender, text = notification.sender, notification.message_text.strip()
    # Если ключевое слово для записи — запускаем сценарий записи (и только его)
    if is_step_keyword(text):
        # Сбросить все данные и начать запись заново
        booking_data[sender] = {}
        conversation_histories[sender] = []
        # Запускать только сценарий записи, не отправлять приветствие
        # Здесь можно сразу перевести пользователя в первый шаг (выбор филиала)
        branches_str = "\n".join([
            f"{i+1}. {b['name']}" for i, b in enumerate(BRANCHES)])
        notification.answer(
            f"Давайте начнём запись! Выберите филиал:\n{branches_str}\nНапишите номер филиала.")
        notification.state_manager.set_state(sender, States.CHOOSE_BRANCH)
        return
    # Если это просто вопрос — отвечаем на него (даже если это первое сообщение)
    history = conversation_histories.setdefault(sender, [])
    history.append({"role": "user", "content": text})
    all_masters, all_services = get_cached_data()
    system_prompt = get_system_prompt(all_masters, all_services)
    booking_data.setdefault(sender, {})['ALL_MASTERS'] = all_masters
    booking_data.setdefault(sender, {})['ALL_SERVICES'] = all_services
    response = get_gpt_response(history[-MAX_HISTORY_MESSAGES:], system_prompt)
    notification.answer(response)
    history.append({"role": "assistant", "content": response})


if __name__ == "__main__":

    bot.run_forever()