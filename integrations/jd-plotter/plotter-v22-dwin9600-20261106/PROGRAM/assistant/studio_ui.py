"""Object editor tools. Every result is a proposal, never a motor command."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from .planner import Plan
from .scene import scene_plan, render_scene
from .studio_commands import block_scene, default_card, use_template, caption


def draw(canvas, paths, w, h):
    canvas.delete('all')
    cw,ch = max(20,canvas.winfo_width()),max(20,canvas.winfo_height())
    scale = min((cw-20)/w,(ch-20)/h)
    if scale<=0:
        return
    ox,oy = (cw-w*scale)/2,(ch-h*scale)/2
    def xy(p):
        return ox+p[0]*scale,oy+p[1]*scale
    canvas.create_rectangle(ox,oy,ox+w*scale,oy+h*scale,fill='white',outline='#a8b6c8')
    last = None
    for path in paths:
        if len(path)<2:
            continue
        if last is not None and last!=path[0]:
            canvas.create_line(*xy(last),*xy(path[0]),fill='#df790d',dash=(3,2))
        for offset in range(0,len(path)-1,512):
            canvas.create_line(*[v for p in path[offset:offset+513] for v in xy(p)],fill='#2563a6')
        last = path[-1]


class StudioPanel:
    def __init__(self,window):
        self.window = window
        self.root = tk.Toplevel(window.root)
        self.root.title('Редактор · объекты, блоки, шаблоны и точность')
        self.root.geometry('880x740')
        self.root.minsize(780,650)
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.note = tk.StringVar(value='Здесь меняется только эскиз. Рисование и движение запускаются отдельно в пульте.')
        tabs = self.tabs = ttk.Notebook(self.root)
        tabs.pack(fill='both',expand=True,padx=12,pady=12)
        self.frames = {}
        for title in ('Текстовый блок','Варианты / маршрут','Шаблоны','История','Точность'):
            frame = ttk.Frame(tabs,padding=12)
            self.frames[title]=frame
            tabs.add(frame,text=title)
        self.build_block()
        self.build_variants()
        self.build_templates()
        self.build_history()
        self.build_accuracy()
        ttk.Label(self.root,textvariable=self.note,wraplength=820).pack(fill='x',padx=14,pady=(0,12))
        self.refresh_history()

    def safe(self,callback):
        try:
            if self.window.busy or self.window.voice.busy or self.window.snapshot().state.get('busy'):
                raise ValueError('Сначала дождитесь окончания текущей операции.')
            callback()
        except (ValueError,OSError,TypeError,KeyError) as exc:
            self.note.set(str(exc))

    def dimensions(self):
        before = self.window.snapshot()
        w,h = before.state.get('sheet_width'),before.state.get('sheet_height')
        if w is None or h is None:
            raise ValueError('Задайте два угла в пульте или выберите пробный лист.')
        return before,w,h

    def propose(self,before,plan):
        self.window.stage_plan(before,plan)
        self.note.set('Предпросмотр готов в помощнике. Проверьте его и нажмите «Применить к эскизу». Станок не запускается.')

    def field(self,parent,row,title,default='',width=16):
        ttk.Label(parent,text=title).grid(row=row,column=0,sticky='w',pady=4)
        var = tk.StringVar(value=str(default))
        ttk.Entry(parent,textvariable=var,width=width).grid(row=row,column=1,sticky='w',padx=8)
        return var

    @staticmethod
    def numeric(var):
        try:
            return float(var.get().strip().replace(',','.'))
        except ValueError:
            raise ValueError('Введите число; дробную часть можно отделять точкой или запятой.') from None

    def build_block(self):
        frame = self.frames['Текстовый блок']
        ttk.Label(frame,text='Русский и латиница · печатный однолинейный шрифт · переносы по словам').pack(anchor='w')
        self.block_text = ScrolledText(frame,height=7,wrap='word')
        self.block_text.pack(fill='x',pady=8)
        self.block_text.insert('1.0','С днём рождения!\nЖелаю радости и вдохновения.')
        form = ttk.Frame(frame)
        form.pack(anchor='w')
        self.block_width = self.field(form,0,'Ширина прямоугольника, мм',80)
        self.block_height = self.field(form,1,'Высота прямоугольника, мм',40)
        self.block_font = self.field(form,2,'Высота заглавных, мм',5)
        self.block_spacing = self.field(form,3,'Интервал букв, мм (пусто = авто)','')
        self.block_pitch = self.field(form,4,'Межстрочный шаг × высоту букв',1.6)
        self.block_align = tk.StringVar(value='Слева')
        ttk.Label(form,text='Выравнивание строк').grid(row=5,column=0,sticky='w')
        ttk.Combobox(form,textvariable=self.block_align,values=('Слева','По центру','Справа'),state='readonly',width=14).grid(row=5,column=1,padx=8)
        self.block_add = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame,text='Добавить отдельным объектом, сохранить остальные',variable=self.block_add).pack(anchor='w',pady=10)
        ttk.Button(frame,text='Построить предпросмотр',command=lambda:self.safe(self.preview_block)).pack(anchor='w')
        ttk.Label(frame,text='Блок появляется в центре; затем выберите его и задайте положение. '
            'При нехватке места выводится конкретная проверенная высота букв. Уменьшать или обрезать текст без вашего выбора программа не будет.',wraplength=780).pack(anchor='w',pady=12)

    def preview_block(self):
        before,w,h = self.dimensions()
        spacing = self.numeric(self.block_spacing) if self.block_spacing.get().strip() else None
        scene = block_scene(self.block_text.get('1.0','end-1c'),self.numeric(self.block_width),self.numeric(self.block_height),w,h,
            self.numeric(self.block_font),spacing,self.numeric(self.block_pitch),
            {'Слева':'left','По центру':'center','Справа':'right'}[self.block_align.get()],
            self.window.current_scene() if self.block_add.get() else None)
        self.propose(before,scene_plan(scene,'Текстовый блок с рассчитанными переносами и выравниванием; остальные объекты сохраняются.' if self.block_add.get() else 'Заменить композицию текстовым блоком.'))

    def command(self,prompt):
        def run():
            self.window.entry.delete('1.0','end')
            self.window.entry.insert('1.0',prompt)
            self.window.send()
        self.safe(run)

    def build_variants(self):
        frame = self.frames['Варианты / маршрут']
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Button(row,text='Показать три оформления',command=lambda:self.command('Сделай три оформления')).pack(side='left')
        ttk.Button(row,text='Оптимизировать маршрут',command=lambda:self.command('Оптимизируй маршрут пера')).pack(side='left',padx=8)
        ttk.Label(frame,text='Оптимизация не изменяет форму штрихов, не удаляет их и не гарантирует глобальный минимум. '
            'Начальная точка сохраняется. Оранжевые переходы без подъёма пера рисуются.',wraplength=790).pack(anchor='w',pady=10)
        self.variant_area = ttk.Frame(frame)
        self.variant_area.pack(fill='both',expand=True)
        self.variant_drawings = []

    def show_variants(self,before,mode,options):
        for child in self.variant_area.winfo_children():
            child.destroy()
        self.variant_drawings=[]
        self.tabs.select(self.frames['Варианты / маршрут'])
        w,h = before.state['sheet_width'],before.state['sheet_height']
        for i,(name,plan) in enumerate(options):
            col = ttk.Frame(self.variant_area,padding=5)
            col.pack(side='left',fill='both',expand=True)
            ttk.Label(col,text=f'{i+1}. {name}',wraplength=230).pack(anchor='w')
            canvas = tk.Canvas(col,width=235,height=350,bg='#dde5ee',highlightthickness=0)
            canvas.pack(fill='both',expand=True,pady=8)
            if isinstance(plan,Plan):
                paths = render_scene(plan.parameters['scene'],w,h)
                canvas.bind('<Configure>',lambda _,c=canvas,p=paths:draw(c,p,w,h))
                self.variant_drawings.append((canvas,paths,w,h))
                ttk.Button(col,text='Выбрать вариант',command=lambda p=plan:self.safe(lambda:self.choose_variant(before,mode,p))).pack()
            else:
                canvas.create_text(115,150,text=str(plan),width=205,justify='left')
                ttk.Button(col,text='Не помещается',state='disabled').pack()
        self.root.lift()

    def choose_variant(self,before,mode,plan):
        self.window.stage_plan(before,plan,mode)
        self.note.set('Вариант выбран для предпросмотра, но ещё не применён. После применения его объекты можно редактировать.')

    def build_templates(self):
        frame = self.frames['Шаблоны']
        ttk.Button(frame,text='Пример открытки',command=lambda:self.safe(self.preview_card)).pack(anchor='w')
        form = ttk.Frame(frame)
        form.pack(anchor='w',pady=8)
        self.template_name = self.field(form,0,'Название своей команды','Моя открытка',28)
        self.recipient = self.field(form,1,'Новое имя (буквально)','Анна',28)
        self.recipient_format = self.field(form,2,'Формат: один {name}','Для {name}',28)
        ttk.Label(frame,text='Поле имени — выбранная в помощнике надпись. Остальные объекты сохраняются. '
            'Если поле имени не выбрано, сохраняется фиксированный шаблон без подстановки. Размер листа фиксируется, скрытого масштабирования нет.',wraplength=790).pack(anchor='w',pady=8)
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Button(row,text='Запомнить текущую композицию',command=lambda:self.safe(self.save_template)).pack(side='left')
        ttk.Button(row,text='Подготовить для имени',command=lambda:self.safe(self.preview_template)).pack(side='left',padx=8)
        self.template_list = tk.Listbox(frame,height=10)
        self.template_list.pack(fill='both',expand=True,pady=10)
        self.template_list.bind('<<ListboxSelect>>',self.template_selected)
        ttk.Button(frame,text='Обновить библиотеку',command=self.refresh_templates).pack(anchor='w')
        self.refresh_templates()
        if self.window.templates is None:
            ttk.Label(frame,text='Библиотека сохраняется при обычном запуске через START.cmd. В тестовом режиме она отключена.').pack(anchor='w')

    def refresh_templates(self):
        self.template_list.delete(0,'end')
        if self.window.templates:
            for name in self.window.templates.names():
                self.template_list.insert('end',name)

    def template_selected(self,event=None):
        items = self.template_list.curselection()
        if items:
            self.template_name.set(self.template_list.get(items[0]))

    def preview_card(self):
        before,w,h = self.dimensions()
        self.propose(before,scene_plan(default_card(w,h,self.recipient.get()),'Открытка с буквальным именем; проверьте текст и переходы перед применением.'))

    def save_template(self):
        if not self.window.templates:
            raise ValueError('Библиотека отключена в тестовом режиме; запустите START.cmd.')
        if self.window.pending or self.window.context_proposal:
            raise ValueError('Сначала примените или отмените предложение. Сохраняется текущая композиция.')
        before,w,h = self.dimensions()
        scene = self.window.current_scene()
        if scene is None:
            raise ValueError('Нет композиции для сохранения.')
        identifier = self.window.selected_objects.get(self.window.mode.get())
        if identifier and caption(next(o for o in scene['objects'] if o['id']==identifier)) is None:
            raise ValueError('Поле имени должно быть печатной надписью, а не рамкой или произвольными контурами.')
        name,fmt = self.template_name.get(),self.recipient_format.get()
        if self.window.templates.path(name).exists():
            if not messagebox.askokcancel('Заменить шаблон?',f'Шаблон «{name}» будет перезаписан.',parent=self.root):
                return
        if before.revision!=self.window.snapshot().revision or self.window.busy or self.window.snapshot().state.get('busy'):
            raise ValueError('Композиция изменилась во время подтверждения.')
        from geometry import Sheet
        self.window.templates.save(name,Sheet(w,h),scene,identifier,fmt)
        self.window.last_templates[self.window.mode.get()] = name.strip()
        self.refresh_templates()
        self.note.set('Шаблон сохранён локально. В чате: «'+name+' для Анны»; имя вставляется буквально, без угадывания падежа.')

    def preview_template(self):
        if not self.window.templates:
            raise ValueError('Библиотека отключена в тестовом режиме.')
        before,w,h = self.dimensions()
        data = self.window.templates.load(self.template_name.get())
        recipient = self.recipient.get() if data['recipient_object'] else None
        self.propose(before,scene_plan(use_template(data,w,h,recipient),'Шаблон '+data['name']+'; имя использовано буквально.'))
        self.window.last_templates[self.window.mode.get()] = data['name']

    def build_history(self):
        frame = self.frames['История']
        ttk.Label(frame,text='До 30 шагов назад. Список относится к выбранному режиму; новая правка очищает ветку повтора. '
            'История не записывает движение, COM или физический ноль.',wraplength=790).pack(anchor='w')
        self.history_list = tk.Listbox(frame,height=20)
        self.history_list.pack(fill='both',expand=True,pady=10)
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Button(row,text='Обновить историю',command=self.refresh_history).pack(side='left')
        ttk.Button(row,text='Вернуться к выбранному',command=lambda:self.safe(self.restore_history)).pack(side='left',padx=8)

    def refresh_history(self):
        mode = self.window.mode.get()
        before = self.window.snapshot()
        if mode=='Эскиз плоттера' and self.window.get_editor_history:
            states,index = self.window.get_editor_history()
            names = [item[1] for item in states]
        else:
            store = self.window.sandbox.history
            states = store.past+[(before.paths,before.state.get('file_name','Эскиз'),self.window.sandbox.sheet)]+list(reversed(store.future))
            index=len(store.past)
            names=[item[1] for item in states]
        self.history_scope = (mode,before.revision,index)
        self.history_list.delete(0,'end')
        for i,name in enumerate(names):
            self.history_list.insert('end',('● ' if i==index else '  ')+str(i)+'. '+str(name)[:130])
        self.history_list.selection_set(index)
        self.history_list.see(index)

    def restore_history(self):
        selected = self.history_list.curselection()
        if not selected:
            return
        mode,revision,index = self.history_scope
        if mode!=self.window.mode.get() or revision!=self.window.snapshot().revision:
            raise ValueError('История изменилась; обновите список.')
        target = selected[0]
        if target==index:
            return
        if not messagebox.askokcancel('Восстановить вариант?', 'Предложение будет отменено. Восстановится только эскиз, станок не запускается.',parent=self.root):
            return
        if self.window.busy or self.window.voice.busy or self.window.snapshot().state.get('busy') or self.window.mode.get()!=mode or self.window.snapshot().revision!=revision:
            raise ValueError('Состояние изменилось во время подтверждения.')
        self.window.cancel_request()
        for _ in range(abs(target-index)):
            old = self.window.snapshot().revision
            (self.window.undo if target<index else self.window.redo)()
            if old==self.window.snapshot().revision:
                raise ValueError('История недоступна; проверьте сообщение помощника.')
        self.refresh_history()
        self.note.set('Вариант восстановлен; можно продолжать редактирование или повторить отменённые шаги.')

    def build_accuracy(self):
        frame = self.frames['Точность']
        ttk.Label(frame,text='1. Сделайте предпросмотр. Первый прогон — без ручки, на малой скорости. '
            '2. Запуск — обычной кнопкой пульта. 3. Измерьте реальные размеры и отклонения возврата.',wraplength=790).pack(anchor='w')
        form = ttk.Frame(frame)
        form.pack(anchor='w',pady=6)
        self.sample_size=self.field(form,0,'Сторона образца, мм',40)
        self.sample_repeats=self.field(form,1,'Циклов туда-обратно (1–10)',5)
        ttk.Button(form,text='Предпросмотр образца',command=lambda:self.safe(self.preview_accuracy)).grid(row=0,column=2,rowspan=2,padx=12)
        self.target_x=self.field(form,2,'Задано вправо, мм',40)
        self.target_y=self.field(form,3,'Задано вниз, мм',40)
        self.measured_x=self.field(form,4,'Измерено вправо, мм','')
        self.measured_y=self.field(form,5,'Измерено вниз, мм','')
        self.current_x=self.field(form,6,'Текущий $100 (GRBL X), шаг/мм','')
        self.current_y=self.field(form,7,'Текущий $101 (GRBL Y), шаг/мм','')
        self.swap=tk.BooleanVar(value=True)
        ttk.Checkbutton(frame,text='Наши оси: портал (вниз) = GRBL X, каретка (вправо) = GRBL Y',variable=self.swap).pack(anchor='w')
        ttk.Button(frame,text='Рассчитать поправки — не записывать',command=lambda:self.safe(self.calculate_accuracy)).pack(anchor='w',pady=6)
        ttk.Label(frame,text='Возвраты: 3–30 строк «вправо; вниз» в мм, например 0,1; -0,05. Это ваши измерения, не датчик.').pack(anchor='w')
        self.return_text=ScrolledText(frame,height=3,width=45)
        self.return_text.pack(fill='x',pady=4)
        ttk.Button(frame,text='Оценить повторяемость',command=lambda:self.safe(self.calculate_returns)).pack(anchor='w')
        self.accuracy_report=ScrolledText(frame,height=6,wrap='word',state='disabled')
        self.accuracy_report.pack(fill='both',expand=True,pady=8)

    def report(self,text):
        self.accuracy_report.configure(state='normal')
        self.accuracy_report.delete('1.0','end')
        self.accuracy_report.insert('1.0',text)
        self.accuracy_report.configure(state='disabled')

    def preview_accuracy(self):
        from .accuracy import accuracy_sample
        before,w,h=self.dimensions()
        count=self.numeric(self.sample_repeats)
        if not count.is_integer():
            raise ValueError('Число повторов должно быть целым.')
        size=self.numeric(self.sample_size)
        plan=accuracy_sample(size,w,h,int(count))
        self.propose(before,plan)
        self.target_x.set(str(size))
        self.target_y.set(str(size))

    def calculate_accuracy(self):
        from .accuracy import correction
        result=correction(*[self.numeric(v) for v in (self.target_x,self.target_y,self.measured_x,self.measured_y,self.current_x,self.current_y)],swap=self.swap.get())
        self.report(f'Ошибка вправо: {result["right_error_mm"]:+.4f} мм; вниз: {result["down_error_mm"]:+.4f} мм.\n'
            f'Расчёт: $100={result["grbl_x_steps_per_mm"]:.6f}, $101={result["grbl_y_steps_per_mm"]:.6f}.\n'
            'Настройки НЕ записаны. Проверьте перестановку осей и исходные значения в пульте. '
            'Измерьте несколько образцов: люфт и пропуски шагов нельзя исправить одним коэффициентом. '
            'После ручного изменения коэффициентов задайте оба угла заново и проверьте образец.')

    def calculate_returns(self):
        from .accuracy import repeatability
        points=[]
        for line in self.return_text.get('1.0','end').strip().splitlines():
            fields=line.split(';')
            if len(fields)!=2:
                raise ValueError('Каждая строка должна содержать два числа через «;».')
            try:
                points.append(tuple(float(v.strip().replace(',','.')) for v in fields))
            except ValueError:
                raise ValueError('Отклонения возврата должны быть числами.') from None
        result=repeatability(points)
        self.report(f'Измерений: {result["sample_count"]}; максимальная ошибка возврата: {result["max_return_error_mm"]:.4f} мм.\n'
            f'Размах вправо: {result["range_right_mm"]:.4f} мм; вниз: {result["range_down_mm"]:.4f} мм.\n'
            f'Среднее вправо: {result["mean_right_mm"]:+.4f} мм; вниз: {result["mean_down_mm"]:+.4f} мм.\n'
            'Это оценка введённых вами измерений, не автоматическое измерение станка. Настройки не менялись.')

    def close(self):
        if self.root.winfo_exists():
            self.root.destroy()
