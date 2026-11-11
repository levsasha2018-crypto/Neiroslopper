"""Deterministic studio programs around the assistant. No motion and no code eval."""
from copy import deepcopy
import math
import re

from .planner import Plan, parse, _VALUE, _parsed_number
from .scene import (from_plan, scene_plan, render_scene, checked_scene, transform_scene,
                    source_paths, new_object, bounds, compose, IDENTITY, GEOMETRY_KINDS)


def get_scene(state):
    draft = state.get('preview_draft')
    if isinstance(draft, Plan) and draft.kind == 'object_scene':
        return deepcopy(draft.parameters['scene'])
    scene = state.get('studio_scene')
    if scene is not None:
        return deepcopy(scene)
    task = state.get('preview_task')
    if isinstance(task, Plan) and task.kind == 'object_scene':
        return deepcopy(task.parameters['scene'])
    if isinstance(task,Plan) and task.kind=='text_block':
        from .planner import transform_paths
        return from_plan(task,transform_paths([],task,state['sheet_width'],state['sheet_height']),state['sheet_width'],state['sheet_height'])
    return None


def caption(obj):
    source = obj['source']
    if source['kind'] in {'print_text', 'text_block', 'text'}:
        return source['parameters']['text']
    if source['kind'] == 'free_preview':
        return next((item['parameters']['text'] for item in source['parameters']['operations']
                     if item['kind'] in {'print_text','text'}), None)
    return None


def set_caption(scene, identifier, literal, w, h):
    result = deepcopy(scene)
    obj = next((o for o in result['objects'] if o['id'] == identifier), None)
    if obj is None:
        raise ValueError('Неизвестный объект имени.')
    source = obj['source']
    if source['kind'] in {'print_text','text_block'}:
        source['parameters']['text'] = literal
    elif source['kind'] == 'free_preview':
        texts = [item for item in source['parameters']['operations'] if item['kind'] == 'print_text']
        if len(texts) != 1:
            raise ValueError('Для поля имени нужна одна печатная надпись.')
        texts[0]['parameters']['text'] = literal
    else:
        raise ValueError('Объект имени не является печатной надписью.')
    obj['name'] = literal[:160] or 'Надпись'
    result['route'] = None
    checked_scene(result,w,h)
    return result


def block_scene(text, bw, bh, w, h, font=5, spacing=None, pitch=1.6, align='left', scene=None):
    result = deepcopy(scene) if scene is not None else {'version':1,'objects':[],'route':None}
    source = {'kind':'text_block','parameters':{'text':text, 'width_mm':bw, 'height_mm':bh,
        'font_height_mm':font, 'spacing_mm':spacing, 'line_spacing':pitch, 'align':align}}
    new_object(result,source,text)
    checked_scene(result,w,h)
    return result


def default_card(w,h,recipient='Анна'):
    if min(w,h)<40:
        raise ValueError('Для стандартной открытки нужен лист хотя бы 40×40 мм.')
    scene = {'version':1,'objects':[],'route':None}
    font = min(8, w/20)
    new_object(scene,{'kind':'print_text','parameters':{'text':'Для '+recipient,'height_mm':font,'spacing_mm':None}},
               'Получатель',[1,0,0,1,0,-h*.32])
    scene = block_scene('С днём рождения!\nЖелаю радости, вдохновения и ярких открытий.',
        w*.76,h*.38,w,h,font=min(5,w/35),align='center',scene=scene)
    new_object(scene,{'kind':'print_text','parameters':{'text':'С наилучшими пожеланиями','height_mm':max(1,min(3,w/55)),'spacing_mm':None}},
               'Подпись',[1,0,0,1,0,h*.34])
    new_object(scene,{'kind':'paths','parameters':{'paths':[[(5,5),(w-5,5),(w-5,h-5),(5,h-5),(5,5)]]}},'Рамка')
    checked_scene(scene,w,h)
    return scene


def use_template(data,w,h,recipient=None):
    if abs(data['sheet_mm'][0]-w)>.01 or abs(data['sheet_mm'][1]-h)>.01:
        raise ValueError('Размер шаблона отличается от листа. Откройте его на пробном листе этого размера; скрытого масштабирования нет.')
    scene = deepcopy(data['scene'])
    if recipient is not None:
        if data['recipient_object'] is None:
            raise ValueError('У шаблона не задан объект имени. Выберите его при сохранении шаблона.')
        scene = set_caption(scene,data['recipient_object'],data['recipient_format'].replace('{name}',recipient),w,h)
    checked_scene(scene,w,h)
    return scene


def variants(scene,w,h,selected=None):
    result = []
    base = transform_scene(scene,Plan('center'),w,h)
    result.append(('Спокойное · по центру',scene_plan(base,'Вариант 1: центрированная композиция, текст без подмены.')))
    try:
        from .drawing_tools import rectangle_frame
        framed = deepcopy(base)
        frame = rectangle_frame(bounds(render_scene(framed,w,h)),3)
        new_object(framed,{'kind':'paths','parameters':{'paths':[frame]}},'Рамка варианта')
        checked_scene(framed,w,h)
        result.append(('Рамка · отступ 3 мм',scene_plan(framed,'Вариант 2: рамка с отступом 3 мм; переходы тоже будут нарисованы.')))
    except ValueError as exc:
        result.append(('Рамка не помещается',str(exc)))
    try:
        target = selected or next((o['id'] for o in base['objects'] if caption(o) is not None),None)
        accented = transform_scene(base,Plan('rotate',{'degrees':-5}),w,h,target)
        result.append(('Акцент · наклон −5°',scene_plan(accented,'Вариант 3: наклон выбранной/первой надписи −5°, остальные элементы сохраняются.')))
    except ValueError as exc:
        result.append(('Акцент не помещается',str(exc)))
    return result


def _choose(scene, target):
    text = target.strip()
    if text.startswith(('«','"','“')) and text[-1:] in ('»','"','”'):
        text = text[1:-1]
    match = re.fullmatch(r'(?:объект(?: номер)?\s+)?([1-9][0-9]?)',text.casefold())
    if match:
        index = int(match[1])-1
        return scene['objects'][index]['id'] if index<len(scene['objects']) else None
    choices = [o['id'] for o in scene['objects'] if o['name']==text or caption(o)==text]
    return choices[0] if len(choices)==1 else None


def respond(prompt,state,cancel=None):
    from .runtime import Reply
    if not isinstance(prompt,str) or not 0<len(prompt)<=1800:
        return None
    if cancel is not None and cancel.is_set():
        return Reply('Запрос отменён.')
    text = re.sub(r'\s+',' ',prompt.casefold().replace('ё','е')).strip(' .!?')
    w,h = state.get('sheet_width'),state.get('sheet_height')
    scene = get_scene(state)
    selected = state.get('studio_selected')
    base_retry = None
    try:
        if scene is not None:
            checked_scene(scene,w,h)
        # Commands must full-match; quoted text containing feature names is never dispatched.
        select = re.fullmatch(r'(?:выбери|выдели)\s+(?:объект\s+)?(.+)',prompt.strip(),re.I)
        if select and scene is not None:
            if select[1].casefold() in {'все','всё','всю композицию','весь рисунок','все объекты'}:
                return Reply('Выбрана вся композиция; геометрия не изменена.',select_all=True)
            identifier = _choose(scene,select[1])
            return Reply('Объект выбран; последующие правки относятся к нему. Изменений пока нет.',selection_id=identifier) if identifier else Reply('Не нашёл один однозначный объект. Выберите его мышкой или номером в списке.')
        if text in {'оптимизируй маршрут','оптимизируй маршрут пера','сократи соединения','оптимизировать маршрут'}:
            if scene is None:
                return Reply('Сначала загрузите эскиз.')
            from .route_opt import optimize_scene
            result,before,after = optimize_scene(scene,w,h,cancel)
            explanation = f'Соединения: {before:.2f} → {after:.2f} мм. Все штрихи сохранены; изменён лишь порядок/направление контуров. Без подъёма соединения всё равно рисуются.'
            return Reply(explanation,plan=scene_plan(result,explanation))
        if text in {'сделай три оформления','сделай 3 оформления','покажи три оформления','покажи 3 варианта','сделай три варианта','сделай 3 варианта'}:
            if scene is None:
                return Reply('Сначала создайте надпись или загрузите рисунок.')
            return Reply('Подготовлены три оформления без подмены текста. Выберите предпросмотр; применение потребует отдельного подтверждения.',variants=variants(scene,w,h,selected))
        match = re.fullmatch(r'(?:создай|размести|напиши|добавь)\s+(?:текст\s+|текстовый блок\s+|надпись\s+)?[«"]([^»"]+)[»"]\s+в\s+(?:прямоугольнике|блоке)\s+(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\s*мм(?:\s+(?:высотой|буквы)\s+(\d+(?:[.,]\d+)?)\s*мм)?',prompt.strip(),re.I|re.S)
        if match:
            bw,bh = float(match[2].replace(',','.')),float(match[3].replace(',','.'))
            font = float(match[4].replace(',','.')) if match[4] else 5
            result = block_scene(match[1],bw,bh,w,h,font,scene=scene if text.startswith('добавь') else None)
            explanation = f'Текстовый блок {bw:g}×{bh:g} мм, заглавные {font:g} мм, переносы по словам, выравнивание слева. Размер не уменьшен автоматически.'
            return Reply(explanation,plan=scene_plan(result,explanation))
        if text in {'создай открытку','сделай открытку'}:
            result = default_card(w,h)
            return Reply('Стандартная открытка с именем «Анна» как редактируемым примером; замените имя до рисования.',
                         plan=scene_plan(result,'Шаблон открытки: имя-пример, поздравление, подпись и рамка. Проверьте все слова.'))
        # Templates were read and validated by the UI.
        template = state.get('studio_template')
        match = re.fullmatch(r'(?:шаблон\s+)?[«"]?(.+?)[»"]?(?:\s+для\s+[«"]?([^»"]+)[»"]?)?',prompt.strip(),re.I)
        if template is not None and match and match[1].strip().casefold()==template['name'].casefold():
            result = use_template(template,w,h,match[2])
            return Reply('Шаблон подготовлен. Имя использовано буквально; падеж не угадывается.',plan=scene_plan(result,'Применить шаблон '+template['name']),template_name=template['name'])
        same = re.fullmatch(r'(?:сделай|создай)\s+(?:такую|такой|такую же|такой же)(?:\s+открытку|\s+шаблон)?\s+для\s+(.+)',prompt.strip(),re.I)
        if same:
            if template is None:
                return Reply('Сначала назовите сохранённый шаблон: «Моя открытка для Анны». Пока не знаю, к какому шаблону относится «такую».')
            recipient=same[1]
            if recipient.startswith(('«','"')) and recipient[-1:] in ('»','"'):
                recipient=recipient[1:-1]
            result=use_template(template,w,h,recipient)
            return Reply('На основе сохранённого шаблона «'+template['name']+'», не несохранённых правок. Имя вставлено буквально.',
                plan=scene_plan(result,'Шаблон '+template['name']+' для '+recipient),template_name=template['name'])
        if scene is None:
            return None
        # Converting legacy paths for hit-testing must not change its mature
        # unselected text parser. Object scenes and explicit selections opt in.
        task = state.get('preview_draft') or state.get('preview_task')
        active = selected is not None or (isinstance(task,Plan) and task.kind in {'object_scene','text_block'})
        original_prompt = prompt
        # Explicit target in a geometry request, including ordinal objects.
        named = re.fullmatch(r'(сдвинь|перенеси|поверни|увеличь|уменьши|отрази)\s+([«"][^»"]+[»"]|(?:объект(?: номер)?\s+\d+)|(?:первый|второй|третий)\s+объект)\s*(.*)',prompt.strip(),re.I)
        if named:
            active = True
            target = named[2]
            ordinal = {'первый объект':'1','второй объект':'2','третий объект':'3'}
            selected = _choose(scene,ordinal.get(target.casefold(),target))
            if selected is None:
                return Reply('Нужно однозначно выбрать объект; одинаковые подписи различайте по номеру.')
            prompt = named[1]+' '+named[3]
            text = prompt.casefold().replace('ё','е').strip(' .!?')
        if not active:
            return None
        # Appending creates a new independent caption, not a destructive edit
        # of the selected text. The same literal parser validates its fields.
        appended = re.fullmatch(r'добавь\s+(?:надпись\s+|текст\s+|печатными\s+)?[«"]([^»"]+)[»"](.*)',prompt.strip(),re.I|re.S)
        if appended:
            command = 'Напиши печатными «'+appended[1]+'»'+appended[2]
            addition = parse(command,state)
            if addition.kind=='print_text':
                result = deepcopy(scene)
                new_object(result,{'kind':addition.kind,'parameters':addition.parameters},appended[1])
                checked_scene(result,w,h)
                return Reply('Добавить отдельную надпись, сохранив остальные объекты.',plan=scene_plan(result,'Добавить «'+appended[1]+'» отдельным объектом.'))
            return Reply('Для новой надписи: «Добавь надпись «Анна» высотой 5 мм». Ничего не изменено.')
        whole = bool(re.search(r'\b(?:рисунок|весь эскиз|всю композицию|все объекты)\b',text))
        if whole:
            selected = None
            prompt = re.sub(r'всю композицию|все объекты|весь эскиз','рисунок',prompt,flags=re.I)
        if selected is not None and selected not in {o['id'] for o in scene['objects']}:
            return Reply('Выбранный объект изменился. Выберите его заново.')
        short = re.fullmatch(r'(?:(?:сдвинь|перенеси)\s+)?(чуть|немного|слегка)\s+(левее|правее|выше|ниже)',text)
        if short:
            dx,dy = {'левее':(-w*.05,0),'правее':(w*.05,0),'выше':(0,-h*.05),'ниже':(0,h*.05)}[short[2]]
            plan = Plan('move_preview',{'dx_mm':dx,'dy_mm':dy},'Для «чуть» выбран сдвиг на 5% стороны листа; это допущение программы.')
        else:
            retry = re.fullmatch(r'(?:тогда|лучше|нет,?)\s+(?:в\s+)?('+_VALUE+r')\s+раз(?:а)?',text)
            if retry and state.get('studio_scale_base') is not None:
                scene = deepcopy(state['studio_scale_base'])
                prompt = 'Увеличь в '+retry[1]+' раз'
            geometry_prompt = prompt
            if selected is not None and re.match(r'^(?:сдвинь|перенеси)\s+(?:вправо|влево|вверх|вниз)\b',prompt,re.I):
                geometry_prompt = re.sub(r'^(сдвинь|перенеси)\s+',r'\1 рисунок ',prompt,flags=re.I)
            plan = parse(geometry_prompt,{**state,'has_paths':True})
        if plan.kind in GEOMETRY_KINDS:
            if plan.kind=='scale':
                base_retry = deepcopy(scene)
            result = transform_scene(scene,plan,w,h,selected)
            scope = 'Выбранный объект' if selected else 'Вся композиция'
            explanation = scope+': '+plan.explanation+' Остальные объекты не меняются.' if selected else scope+': '+plan.explanation
            return Reply(explanation,plan=scene_plan(result,explanation),selection_id=selected,studio_base=base_retry,context_request=original_prompt)
        if plan.kind=='frame':
            from .drawing_tools import rectangle_frame
            _,groups = checked_scene(scene,w,h)
            paths = [path for obj,group in zip(scene['objects'],groups) if selected is None or obj['id']==selected for path in group]
            result = deepcopy(scene)
            new_object(result,{'kind':'paths','parameters':{'paths':[rectangle_frame(bounds(paths),plan.parameters['padding_mm'])]}},'Рамка')
            checked_scene(result,w,h)
            return Reply(plan.explanation,plan=scene_plan(result,plan.explanation))
        # Text property/content edits reuse the tested literal-text editor for one source only.
        from .text_editing import _tokens, _typed_request, _choose as choose_caption
        normalized,literals = _tokens(prompt)
        requested = _typed_request(normalized,literals,state.get('awaiting_text_field'))
        if requested is not None and requested[1] is not None and requested[1] not in {'текст','надпись','слово','буквы','шрифт'}:
            action,target,value = requested
            text_objects = [o for o in scene['objects'] if caption(o) is not None]
            fake = [{'kind':'print_text','parameters':{'text':caption(o),'height_mm':5,'spacing_mm':None}} for o in text_objects]
            index = choose_caption(target,list(range(len(fake))),fake,literals)
            if index is None:
                return Reply('Не нашёл однозначную надпись для этой правки. Выберите её мышкой или по номеру объекта.')
            selected = text_objects[index]['id']
            if action=='text':
                # Use the original literal, never casefolded command tokens.
                prompt='Замени текст на «'+value+'»'
            elif action in {'height_mm','spacing_mm'}:
                prompt=('Высота букв ' if action=='height_mm' else 'Интервал между буквами ')+str(value)+' мм'
            elif action in {'upper','lower'}:
                prompt='Сделай текст '+('заглавными' if action=='upper' else 'строчными')
            elif action=='await':
                prompt={'text':'Замени текст','height_mm':'Измени высоту букв','spacing_mm':'Измени интервал между буквами'}[value]
        candidates = [o for o in scene['objects'] if caption(o) is not None and (selected is None or o['id']==selected)]
        if not candidates:
            return None
        from .text_editing import edit
        if re.fullmatch(r'(?:что за текст|какой текст|что написано|какая надпись)(?: сейчас)?[?]?',text):
            return Reply('Сохранённые надписи, не измерение станка:\n'+'\n'.join(
                f'{i+1}. «{caption(o)}»'+(' — выбранный объект' if o['id']==selected else '')
                for i,o in enumerate(scene['objects']) if caption(o) is not None))
        if len(candidates)!=1:
            from .text_editing import _looks_like_edit, _tokens
            normalized,_ = _tokens(prompt)
            if _looks_like_edit(normalized) or re.fullmatch(r'(?:сделай\s+)?(?:все\s+)?буквы\s+(?:заглавными|строчными)',normalized):
                return Reply('В композиции несколько надписей. Сначала выберите одну мышкой или командой «Выбери объект 2».')
            return None
        obj = candidates[0]
        source = obj['source']
        is_block = source['kind']=='text_block'
        source_plan = Plan(source['kind'],deepcopy(source['parameters']))
        if is_block:
            p = source['parameters']
            source_plan = Plan('print_text',{'text':p['text'],'height_mm':p['font_height_mm'],'spacing_mm':p['spacing_mm']})
            alignment = re.fullmatch(r'(?:выровняй|выравнивание)\s+(?:текст\s+)?(слева|по центру|справа)',text)
            if alignment:
                result = deepcopy(scene)
                target = next(o for o in result['objects'] if o['id']==obj['id'])
                target['source']['parameters']['align'] = {'слева':'left','по центру':'center','справа':'right'}[alignment[1]]
                result['route']=None
                checked_scene(result,w,h)
                return Reply('Выравнивание строк блока изменится.',plan=scene_plan(result,'Выравнивание строк: '+alignment[1]))
        from .dialog_context import normalize_followup
        prompt = normalize_followup(prompt,state)
        edited = edit(prompt,{**state,'preview_draft':None,'preview_task':source_plan,'editor_paths':source_paths(source,w,h)})
        if edited is None:
            return None
        if not edited.requires_confirmation:
            return Reply(edited.question or edited.explanation,waiting_field=edited.parameters.get('awaiting_text_field',''),
                         waiting_edit={key:edited.parameters[key] for key in ('awaiting_text_request','awaiting_text_target') if key in edited.parameters},context_request=original_prompt)
        result = deepcopy(scene)
        target = next(o for o in result['objects'] if o['id']==obj['id'])
        if is_block:
            if any(item['kind']!='print_text' for item in edited.parameters['operations']):
                raise ValueError('Правки букв блока и его геометрии задавайте отдельно: сначала текст/высота, затем поворот/положение. Никакая часть команды не отброшена.')
            generator = next(item for item in edited.parameters['operations'] if item['kind']=='print_text')['parameters']
            target['source']['parameters'].update(text=generator['text'],font_height_mm=generator['height_mm'],spacing_mm=generator['spacing_mm'])
        else:
            target['source']={'kind':edited.kind,'parameters':deepcopy(edited.parameters)}
        target['name'] = (caption(target) or target['name'])[:160]
        # Absolute letter height cancels this object's uniform external scale only,
        # never a shared scale of the rest of the composition.
        if re.search(r'высот|высотой|размером',re.sub(r'[«"][^»"]*[»"]','',text)):
            matrix = target['matrix']
            scale = math.hypot(matrix[0],matrix[1])
            if abs(math.hypot(matrix[2],matrix[3])-scale)>.0001:
                raise ValueError('Для точной высоты у объекта должен быть равномерный масштаб.')
            old_paths = render_scene({'version':1,'objects':[obj],'route':None},w,h)
            old_bounds = bounds(old_paths)
            target['matrix']=[v/scale for v in matrix[:4]]+matrix[4:]
            # Center of actual ink remains at the same position after absolute resizing.
            from .scene import apply_matrix
            new_bounds = bounds(apply_matrix(source_paths(target['source'],w,h),target['matrix']))
            target['matrix'][4]+=(old_bounds[0]+old_bounds[2]-new_bounds[0]-new_bounds[2])/2
            target['matrix'][5]+=(old_bounds[1]+old_bounds[3]-new_bounds[1]-new_bounds[3])/2
        result['route']=None
        checked_scene(result,w,h)
        return Reply(edited.explanation,plan=scene_plan(result,edited.explanation[:450]),selection_id=selected,context_request=original_prompt)
    except (ValueError,TypeError,KeyError,OverflowError) as exc:
        return Reply('Не применено: '+str(exc)+' Последний корректный эскиз сохранён.',studio_base=base_retry,context_request=prompt,
                     validation_error=str(exc)[:300])
