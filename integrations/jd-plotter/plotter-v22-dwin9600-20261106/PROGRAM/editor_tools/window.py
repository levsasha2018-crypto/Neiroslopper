"""Manual, deterministic sketch editor. No language model, voice or code execution."""
from copy import deepcopy
from pathlib import Path
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from geometry import Sheet
from editor_project import SketchProject, save_project, load_project
from .planner import Plan, transform_paths
from .scene import from_plan, render_scene, scene_plan, new_object, transform_scene, hit_test
from .studio_store import Templates

ANCHORS={'В центре':'center','Сверху слева':'top_left','Сверху справа':'top_right',
         'Снизу слева':'bottom_left','Снизу справа':'bottom_right'}


def draw(canvas, paths, w, h):
    canvas.delete('all')
    cw,ch=max(20,canvas.winfo_width()),max(20,canvas.winfo_height())
    scale=min((cw-28)/w,(ch-28)/h)
    ox,oy=(cw-w*scale)/2,(ch-h*scale)/2
    canvas.create_rectangle(ox,oy,ox+w*scale,oy+h*scale,fill='white',outline='#9aa9ba')
    last=None
    for path in paths:
        if len(path)<2:continue
        if last is not None and last!=path[0]:
            canvas.create_line(ox+last[0]*scale,oy+last[1]*scale,ox+path[0][0]*scale,oy+path[0][1]*scale,fill='#df790d',dash=(3,2))
        for start in range(0,len(path)-1,512):
            canvas.create_line(*[v for x,y in path[start:start+513] for v in (ox+x*scale,oy+y*scale)],fill='#2563a6')
        last=path[-1]
    return ox,oy,scale


class EditorWindow:
    def __init__(self, app):
        self.app=app;self.root=tk.Toplevel(app.root)
        self.root.title('Редактор эскиза · ручные инструменты')
        self.root.geometry('1120x790');self.root.minsize(940,670)
        self.closed=self.busy=False;self.pending=None;self.context_proposal=None
        self.data_dir=app.data_dir;self.reference_panel=None
        self.mode=tk.StringVar(value='Эскиз плоттера');self.selected_objects={}
        self.autosave_enabled=tk.BooleanVar(value=True)
        self.templates=Templates(Path(app.data_dir)/'templates') if app.data_dir else None
        if app.drawing_sheet() is None:
            app.draft_sheet=Sheet(210,297);app.paper_text.set(app.draft_notice());app.redraw()
        self.status=tk.StringVar(value='Изменения применяются только к эскизу. Запуск рисования — в главном пульте.')
        outer=ttk.Frame(self.root,padding=12);outer.pack(fill='both',expand=True)
        toolbar=ttk.Frame(outer);toolbar.pack(fill='x')
        for text,cmd in [('Открыть проект',self.open_project),('Сохранить проект',self.save_project),
                         ('Сохранить SVG',self.save_svg),('Отменить',self.undo),('Повторить',self.redo),
                         ('Рисунок по образцу',self.open_reference)]:
            ttk.Button(toolbar,text=text,command=lambda c=cmd:self.safe(c)).pack(side='left',padx=(0,5))
        columns=ttk.Panedwindow(outer,orient='horizontal');columns.pack(fill='both',expand=True,pady=10)
        left=ttk.Frame(columns,width=355);right=ttk.Frame(columns);columns.add(left,weight=0);columns.add(right,weight=1)
        self.tabs=ttk.Notebook(left);self.tabs.pack(fill='both',expand=True)
        self.dialog_tab=ttk.Frame(self.tabs,padding=10);self.tabs.add(self.dialog_tab,text='Текст')
        transforms=ttk.Frame(self.tabs,padding=10);self.tabs.add(transforms,text='Правки')
        shapes=ttk.Frame(self.tabs,padding=10);self.tabs.add(shapes,text='Фигуры')
        library=ttk.Frame(self.tabs,padding=10);self.tabs.add(library,text='Проект')
        self.build_text(self.dialog_tab);self.build_transforms(transforms);self.build_shapes(shapes);self.build_library(library)
        ttk.Label(right,text='Выбранный объект (или нажмите на его линии)').pack(anchor='w')
        self.selection=tk.StringVar(value='Вся композиция')
        self.object_picker=ttk.Combobox(right,textvariable=self.selection,state='readonly')
        self.object_picker.pack(fill='x',pady=(3,8));self.object_picker.bind('<<ComboboxSelected>>',self.select_object)
        self.canvas=tk.Canvas(right,bg='#dde5ee',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True);self.canvas.bind('<Configure>',lambda _:self.redraw())
        self.canvas.bind('<Button-1>',self.select_at)
        row=ttk.Frame(right);row.pack(fill='x',pady=(8,0))
        self.apply_button=ttk.Button(row,text='Применить к эскизу',command=lambda:self.safe(self.apply));self.apply_button.pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='Отменить предпросмотр',command=self.cancel_preview).pack(side='left',padx=(6,0))
        ttk.Label(outer,textvariable=self.status,wraplength=1030).pack(fill='x')
        self.root.protocol('WM_DELETE_WINDOW',self.close);self.refresh()

    def field(self,frame,row,label,value):
        ttk.Label(frame,text=label,wraplength=190).grid(row=row,column=0,sticky='w',pady=4)
        var=tk.StringVar(value=str(value));ttk.Entry(frame,textvariable=var,width=12).grid(row=row,column=1,sticky='w',padx=6)
        return var

    @staticmethod
    def number(var):
        try:value=float(var.get().strip().replace(',','.'))
        except ValueError:raise ValueError('Введите число в миллиметрах или процентах.') from None
        if not math.isfinite(value):raise ValueError('Нужно конечное число.')
        return value

    def safe(self,callback):
        try:
            if self.app.controller.busy or self.app.svg_loading:raise ValueError('Дождитесь завершения операции в пульте.')
            callback()
        except (ValueError,OSError,TypeError,KeyError) as exc:self.status.set(str(exc))

    def snapshot(self):return self.app.editor_snapshot()

    def dimensions(self):
        before=self.snapshot();return before,before.state['sheet_width'],before.state['sheet_height']

    def current_scene(self):
        if self.pending and self.pending[0].revision==self.snapshot().revision:
            return deepcopy(self.pending[1].parameters['scene'])
        before,w,h=self.dimensions()
        if not before.paths:return {'version':1,'objects':[],'route':None}
        return from_plan(self.app.editor_task,before.paths,w,h)

    def build_text(self,frame):
        ttk.Label(frame,text='Русский и латиница · печатный однолинейный шрифт',wraplength=310).pack(anchor='w')
        self.text=ScrolledText(frame,height=7,wrap='word');self.text.pack(fill='x',pady=7);self.text.insert('1.0','Привет, мир!\nHello, world!')
        form=ttk.Frame(frame);form.pack(anchor='w')
        self.font_height=self.field(form,0,'Высота заглавных, мм',5)
        self.spacing=self.field(form,1,'Интервал букв (пусто — авто)','')
        self.block_width=self.field(form,2,'Ширина блока, мм',80)
        self.block_height=self.field(form,3,'Высота блока, мм',40)
        self.block=tk.BooleanVar(value=False);self.add_text=tk.BooleanVar(value=True)
        ttk.Checkbutton(frame,text='Переносить слова внутри блока',variable=self.block).pack(anchor='w',pady=5)
        ttk.Checkbutton(frame,text='Добавить объект, сохранить остальные',variable=self.add_text).pack(anchor='w')
        self.anchor=tk.StringVar(value='В центре')
        ttk.Combobox(frame,textvariable=self.anchor,values=tuple(ANCHORS),state='readonly').pack(fill='x',pady=8)
        ttk.Button(frame,text='Предпросмотр надписи',command=lambda:self.safe(self.preview_text)).pack(fill='x')
        ttk.Button(frame,text='Заменить текст выбранного объекта',command=lambda:self.safe(self.replace_text)).pack(fill='x',pady=5)

    def preview_text(self):
        before,w,h=self.dimensions();text=self.text.get('1.0','end-1c')
        spacing=self.number(self.spacing) if self.spacing.get().strip() else None
        scene=self.current_scene() if self.add_text.get() else {'version':1,'objects':[],'route':None}
        if self.block.get():
            source={'kind':'text_block','parameters':{'text':text,'width_mm':self.number(self.block_width),'height_mm':self.number(self.block_height),
                    'font_height_mm':self.number(self.font_height),'spacing_mm':spacing,'line_spacing':1.6,'align':'left'}}
        else:source={'kind':'print_text','parameters':{'text':text,'height_mm':self.number(self.font_height),'spacing_mm':spacing}}
        identifier=new_object(scene,source,text)
        if self.anchor.get()!='В центре':scene=transform_scene(scene,Plan('place_preview',{'anchor':ANCHORS[self.anchor.get()],'margin_mm':3}),w,h,identifier)
        self.stage_plan(before,scene_plan(scene,'Печатный текст'))

    def replace_text(self):
        before,w,h=self.dimensions();identifier=self.selected_objects.get(self.mode.get())
        scene=self.current_scene();obj=next((o for o in scene['objects'] if o['id']==identifier),None)
        if obj is None or obj['source']['kind'] not in {'print_text','text_block'}:raise ValueError('Выберите текстовый объект.')
        obj['source']['parameters']['text']=self.text.get('1.0','end-1c');obj['name']=self.text.get('1.0','end-1c')[:160]
        scene['route']=None;self.stage_plan(before,scene_plan(scene,'Изменение текста выбранного объекта'))

    def build_transforms(self,frame):
        self.scale=self.field(frame,0,'Масштаб, %',100);self.angle=self.field(frame,1,'Угол, градусы',0)
        self.dx=self.field(frame,2,'Сдвиг вправо, мм',0);self.dy=self.field(frame,3,'Сдвиг вниз, мм',0)
        self.width=self.field(frame,4,'Ширина рисунка, мм',80)
        actions=[('Масштабировать',lambda:Plan('scale',{'percent':self.number(self.scale)})),
                 ('Повернуть',lambda:Plan('rotate',{'degrees':self.number(self.angle)})),
                 ('Сдвинуть',lambda:Plan('move_preview',{'dx_mm':self.number(self.dx),'dy_mm':self.number(self.dy)})),
                 ('Задать ширину',lambda:Plan('resize_width',{'width_mm':self.number(self.width)})),
                 ('По центру',lambda:Plan('center')),('Отразить по горизонтали',lambda:Plan('mirror',{'axis':'horizontal'})),
                 ('Отразить по вертикали',lambda:Plan('mirror',{'axis':'vertical'}))]
        for row,(label,plan) in enumerate(actions,5):
            ttk.Button(frame,text=label,command=lambda p=plan:self.safe(lambda:self.transform(p()))).grid(row=row,column=0,columnspan=2,sticky='ew',pady=3)
        ttk.Button(frame,text='Удалить выбранный объект',command=lambda:self.safe(self.delete_object)).grid(row=12,column=0,columnspan=2,sticky='ew',pady=5)
        ttk.Button(frame,text='Сократить переходы пера',command=lambda:self.safe(self.optimize)).grid(row=13,column=0,columnspan=2,sticky='ew')

    def transform(self,plan):
        before,w,h=self.dimensions();scene=transform_scene(self.current_scene(),plan,w,h,self.selected_objects.get(self.mode.get()))
        self.stage_plan(before,scene_plan(scene,'Изменение положения или размера'))

    def delete_object(self):
        before,w,h=self.dimensions();identifier=self.selected_objects.get(self.mode.get());scene=self.current_scene()
        if identifier is None:raise ValueError('Выберите отдельный объект.')
        if len(scene['objects'])<=1:raise ValueError('Это последний объект; откройте другой рисунок или замените композицию.')
        scene['objects']=[o for o in scene['objects'] if o['id']!=identifier];scene['route']=None
        self.selected_objects.pop(self.mode.get(),None);self.stage_plan(before,scene_plan(scene,'Удалить объект'))

    def optimize(self):
        from .route_opt import optimize_scene
        before,w,h=self.dimensions();scene,a,b=optimize_scene(self.current_scene(),w,h)
        self.stage_plan(before,scene_plan(scene,f'Переходы: {a:.1f} → {b:.1f} мм'))

    def build_shapes(self,frame):
        self.shape_width=self.field(frame,0,'Ширина / длина, мм',40);self.shape_height=self.field(frame,1,'Высота, мм',30)
        self.shape=tk.StringVar(value='Прямоугольник')
        ttk.Combobox(frame,textvariable=self.shape,values=('Прямоугольник','Эллипс','Треугольник','Линейка','Тест точности'),state='readonly').grid(row=2,column=0,columnspan=2,sticky='ew',pady=8)
        ttk.Button(frame,text='Добавить фигуру',command=lambda:self.safe(self.preview_shape)).grid(row=3,column=0,columnspan=2,sticky='ew')
        ttk.Label(frame,text='Фигуры создаются по центру; положение можно изменить на вкладке «Правки».',wraplength=310).grid(row=4,column=0,columnspan=2,sticky='w',pady=10)

    def preview_shape(self):
        before,w,h=self.dimensions();sw,sh=self.number(self.shape_width),self.number(self.shape_height)
        if not 0<sw<=w or not 0<sh<=h:raise ValueError('Размер фигуры должен помещаться в лист.')
        x,y=(w-sw)/2,(h-sh)/2;kind=self.shape.get()
        if kind=='Прямоугольник':paths=[[(x,y),(x+sw,y),(x+sw,y+sh),(x,y+sh),(x,y)]]
        elif kind=='Треугольник':paths=[[(x+sw/2,y),(x+sw,y+sh),(x,y+sh),(x+sw/2,y)]]
        elif kind=='Эллипс':paths=[[(x+sw/2+sw/2*math.cos(i*math.tau/96),y+sh/2+sh/2*math.sin(i*math.tau/96)) for i in range(97)]]
        else:paths=transform_paths([],Plan('ruler',{'length_mm':sw}) if kind=='Линейка' else Plan('test_pattern',{'size_mm':sw}),w,h)
        scene=self.current_scene();new_object(scene,{'kind':'paths','parameters':{'paths':paths}},kind)
        self.stage_plan(before,scene_plan(scene,kind))

    def build_library(self,frame):
        form=ttk.Frame(frame);form.pack(anchor='w')
        sheet=self.app.drawing_sheet();self.paper_w=self.field(form,0,'Пробный лист: ширина, мм',sheet.width);self.paper_h=self.field(form,1,'Высота, мм',sheet.height)
        ttk.Button(frame,text='Задать пробный лист',command=lambda:self.safe(self.set_paper)).pack(fill='x',pady=8)
        ttk.Label(frame,text='Пробный лист позволяет редактировать без оборудования. Он не задаёт ноль станка.',wraplength=310).pack(anchor='w')
        self.template_name=tk.StringVar(value='Мой шаблон');ttk.Entry(frame,textvariable=self.template_name).pack(fill='x',pady=(18,5))
        ttk.Button(frame,text='Сохранить как шаблон',command=lambda:self.safe(self.save_template)).pack(fill='x')
        ttk.Button(frame,text='Открыть шаблон',command=lambda:self.safe(self.open_template)).pack(fill='x',pady=5)
        ttk.Button(frame,text='Восстановить автокопию',command=lambda:self.safe(self.restore_autosave)).pack(fill='x',pady=12)
        ttk.Label(frame,text='Сохранённые проекты из исходной программы лежат в EXAMPLES / SAVED_PROJECTS.',wraplength=310).pack(anchor='w')

    def set_paper(self):
        if self.app.zero or self.app.sheet:raise ValueError('Сначала сбросьте физическую калибровку в пульте.')
        sheet=Sheet(self.number(self.paper_w),self.number(self.paper_h))
        from geometry import validate_paths
        if self.app.paths:validate_paths(self.app.paths,sheet)
        self.app.draft_sheet=sheet;self.app.loaded(preserve_history=True);self.app.paper_text.set(self.app.draft_notice());self.refresh()

    def stage_plan(self,before,plan,mode=None):
        w,h=before.state['sheet_width'],before.state['sheet_height'];transform_paths(before.paths,plan,w,h)
        self.pending=(before,plan);self.context_proposal=plan
        self.status.set(plan.explanation+' · проверьте линии и нажмите «Применить к эскизу».');self.refresh()

    def apply(self):
        if self.pending is None:raise ValueError('Нет подготовленного изменения.')
        before,plan=self.pending;self.app.apply_editor_preview(before,plan);self.pending=None;self.context_proposal=None;self.refresh()

    def cancel_preview(self):self.pending=None;self.context_proposal=None;self.refresh();self.status.set('Предпросмотр отменён.')
    def append(self,text):self.status.set(text.strip())
    def editor_changed(self):self.pending=None;self.context_proposal=None;self.refresh()
    def undo(self):self.app.undo_editor_preview();self.refresh()
    def redo(self):self.app.redo_editor_preview();self.refresh()

    def open_project(self):
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('Проект плоттера','*.plotter.json'),('JSON','*.json')])
        if path:self.app.load_editor_project(load_project(path));self.refresh()

    def save_project(self):
        if self.pending:raise ValueError('Сначала примените или отмените предпросмотр.')
        sheet=self.app.drawing_sheet();path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.plotter.json',filetypes=[('Проект','*.plotter.json')])
        if path:save_project(path,sheet,self.app.paths,self.app.file_name[:500],self.app.editor_task,self.app.editor_anchor);self.status.set('Проект сохранён.')

    def save_svg(self):
        if self.pending:raise ValueError('Сначала примените или отмените предпросмотр.')
        from .svg_export import save_svg
        sheet=self.app.drawing_sheet();path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.svg',filetypes=[('SVG','*.svg')])
        if path:save_svg(path,self.app.paths,sheet.width,sheet.height,self.app.file_name);self.status.set('SVG сохранён.')

    def save_template(self):
        if not self.templates:raise ValueError('Библиотека доступна при запуске через START.cmd.')
        if self.pending:raise ValueError('Сначала примените предпросмотр.')
        scene=self.current_scene();self.templates.save(self.template_name.get(),self.app.drawing_sheet(),scene);self.status.set('Шаблон сохранён.')

    def open_template(self):
        if not self.templates:raise ValueError('Библиотека недоступна.')
        before,w,h=self.dimensions();data=self.templates.load(self.template_name.get())
        if any(abs(a-b)>.01 for a,b in zip(data['sheet_mm'],(w,h))):raise ValueError('Размер шаблона отличается от текущего листа.')
        self.stage_plan(before,scene_plan(data['scene'],'Открыть шаблон'))

    def restore_autosave(self):
        if not self.app.autosaves:raise ValueError('Нет хранилища автокопий.')
        self.app.load_editor_project(self.app.autosaves.restore('Эскиз плоттера'));self.refresh()

    def open_reference(self):
        try:
            from .reference_ui import ReferencePanel
        except ImportError:raise ValueError('Для обводки установите компоненты через INSTALL_CAMERA.cmd.') from None
        if self.reference_panel is None or self.reference_panel.closed:self.reference_panel=ReferencePanel(self)
        self.reference_panel.root.deiconify();self.reference_panel.root.lift()

    def select_object(self,_event=None):
        index=self.object_picker.current();scene=self.current_scene()
        if index>0:self.selected_objects[self.mode.get()]=scene['objects'][index-1]['id']
        else:self.selected_objects.pop(self.mode.get(),None)
        self.redraw()

    def select_at(self,event):
        if not getattr(self,'preview_transform',None):return
        ox,oy,scale=self.preview_transform;before,w,h=self.dimensions();scene=self.current_scene()
        if not scene['objects']:return
        identifier=hit_test(scene,w,h,((event.x-ox)/scale,(event.y-oy)/scale),max(.5,6/scale))
        if identifier:self.selected_objects[self.mode.get()]=identifier
        else:self.selected_objects.pop(self.mode.get(),None)
        self.refresh()

    def refresh(self):
        if self.closed:return
        scene=self.current_scene();identifier=self.selected_objects.get(self.mode.get())
        self.object_picker['values']=['Вся композиция']+[o['name'] for o in scene['objects']]
        index=next((i+1 for i,o in enumerate(scene['objects']) if o['id']==identifier),0);self.object_picker.current(index)
        self.apply_button.configure(state='normal' if self.pending else 'disabled');self.redraw()

    def redraw(self):
        if self.closed:return
        before,w,h=self.dimensions();scene=self.current_scene();paths=render_scene(scene,w,h) if scene['objects'] else []
        self.preview_transform=draw(self.canvas,paths,w,h)
        identifier=self.selected_objects.get(self.mode.get());obj=next((o for o in scene['objects'] if o['id']==identifier),None)
        if obj:
            from .scene import bounds
            one=deepcopy(scene);one['objects']=[obj];one['route']=None;x0,y0,x1,y1=bounds(render_scene(one,w,h));ox,oy,scale=self.preview_transform
            self.canvas.create_rectangle(ox+x0*scale,oy+y0*scale,ox+x1*scale,oy+y1*scale,outline='#d07816',dash=(4,3),width=2)

    def close(self):
        if self.closed:return
        self.closed=True
        if self.reference_panel is not None:self.reference_panel.close()
        self.root.destroy()
