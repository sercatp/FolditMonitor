import tkinter as tk

class TooltipWindow:
    def __init__(self, parent, fonts, update_interval=1000, hover_delay=200):
        """
        Инициализация окна подсказки
        
        Args:
            parent: Родительское окно
            fonts: Словарь шрифтов {'tooltip': font_object}
            update_interval: Интервал обновления в миллисекундах
            hover_delay: Задержка появления подсказки в миллисекундах
        """
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', False)
        
        self.label = tk.Label(
            self.window,
            justify=tk.LEFT,
            background="#ffffe0",
            relief='solid',
            borderwidth=1,
            font=fonts['tooltip']
        )
        self.label.pack()
        
        # Состояние
        self.visible = False
        self.current_item = None
        self.last_update = 0
        self.update_interval = update_interval
        self.current_x = 0
        self.current_y = 0
        self.update_scheduled = None
        self.hover_delay = hover_delay
        self.pending_show = None
        self.hover_item = None
        self.hover_column = None
        self.current_tree = None
        
        # Ссылка на родительское окно
        self.parent = parent
        self.parent.bind('<FocusOut>', self.on_parent_focus_out)

    def on_parent_focus_out(self, event):
        """Обработка потери фокуса родительским окном"""
        self.hide()

    def show(self, text, x, y, item):
        """
        Показать подсказку
        
        Args:
            text: Текст подсказки
            x: X-координата
            y: Y-координата
            item: Идентификатор элемента
        """
        self.current_x = x
        self.current_y = y
        if self.current_item != item:
            self.current_item = item
            self.window.attributes('-topmost', True)
            self.update_tooltip(text)
            self.schedule_update()

    def update_tooltip(self, text):
        """Обновить содержимое подсказки"""
        self.label.config(text=text, wraplength=0)
        self.window.update_idletasks()
        tooltip_width = self.window.winfo_reqwidth()
        self.window.geometry(f"+{self.current_x - tooltip_width}+{self.current_y}")
        if not self.visible:
            self.window.deiconify()
            self.visible = True

    def hide(self):
        """Скрыть подсказку"""
        if self.pending_show:
            self.window.after_cancel(self.pending_show)
            self.pending_show = None
        if self.update_scheduled:
            self.window.after_cancel(self.update_scheduled)
            self.update_scheduled = None
        self.window.attributes('-topmost', False)
        self.window.withdraw()
        self.visible = False
        self.current_item = None

    def delayed_show(self, text, x, y, item, column, tree_widget=None):
        """Показать подсказку с задержкой"""
        if self.pending_show:
            self.window.after_cancel(self.pending_show)
        
        self.hover_item = item
        self.hover_column = column
        self.current_tree = tree_widget
        
        if tree_widget and hasattr(tree_widget, 'master'):
            tree_frame = tree_widget.master
            y = tree_frame.winfo_rooty()
        
        if not self.visible:
            self.pending_show = self.window.after(
                self.hover_delay,
                lambda: self.check_and_show(text, x, y, item)
            )
        else:
            self.show(text, x, y, item)

    def check_and_show(self, text, x, y, item):
        """Проверить позицию мыши перед показом подсказки"""
        mouse_x = self.current_tree.winfo_pointerx() - self.current_tree.winfo_rootx()
        mouse_y = self.current_tree.winfo_pointery() - self.current_tree.winfo_rooty()
        
        current_column = self.current_tree.identify_column(mouse_x)
        current_item = self.current_tree.identify('item', mouse_x, mouse_y)
        
        if (current_item == self.hover_item and 
            current_column == self.hover_column and 
            current_column == "#1"):
            self.show(text, x, y, item)

    def schedule_update(self):
        """Запланировать следующее обновление подсказки"""
        if self.update_callback and self.visible and self.current_item:
            try:
                if self.current_tree and self.current_item in self.current_tree.get_children():
                    if self.update_scheduled:
                        self.window.after_cancel(self.update_scheduled)
                    
                    text = self.update_callback(self.current_item)
                    if text:
                        self.update_tooltip(text)
                    
                    self.update_scheduled = self.window.after(
                        self.update_interval,
                        self.schedule_update
                    )
            except Exception as e:
                print(f"Error updating tooltip: {str(e)}")

    def set_update_callback(self, callback):
        """
        Установить функцию обновления содержимого подсказки
        
        Args:
            callback: Функция, принимающая item_id и возвращающая текст подсказки
        """
        self.update_callback = callback 
