# image_viewer.py
import tkinter as tk
from tkinter import ttk, Toplevel, messagebox, Listbox
import os
try:
    from PIL import Image, ImageTk
except ImportError:
    messagebox.showerror("Error", "Pillow library not found. Please run 'pip install Pillow'")

class ImageViewer(Toplevel):
    def __init__(self, master, config_manager): 
        super().__init__(master)
        self.title("Image Viewer")
        self.geometry("1600x800")

        self.base_image_dir = config_manager.get_config_value("ImagePath")
        if not self.base_image_dir:
            messagebox.showerror("Error", "ImagePath not found in config file.")
            self.destroy()
            return

        self.image_paths = []
        self.pil_image = None
        self.tk_image = None
        self.zoom_factor = 1.0
        self.view_mode = tk.StringVar(value="All")
        self.sort_mode = tk.StringVar(value="name")

        self.pan_sensitivity = 0.05

        paned_window = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True)

        list_frame = ttk.Frame(paned_window, width=350)
        paned_window.add(list_frame, weight=1)

        mode_frame = ttk.LabelFrame(list_frame, text="View Mode", padding=5)
        mode_frame.pack(fill=tk.X, padx=5, pady=(5, 0))
        ttk.Radiobutton(mode_frame, text="All", variable=self.view_mode, value="All", command=self.load_image_list).pack(side=tk.LEFT, expand=True)
        ttk.Radiobutton(mode_frame, text="Produce", variable=self.view_mode, value="ByProduce", command=self.load_image_list).pack(side=tk.LEFT, expand=True)
        ttk.Radiobutton(mode_frame, text="Analysis", variable=self.view_mode, value="ByAnalysis", command=self.load_image_list).pack(side=tk.LEFT, expand=True)
        ttk.Radiobutton(mode_frame, text="Contour", variable=self.view_mode, value="Contour", command=self.load_image_list).pack(side=tk.LEFT, expand=True)
        ttk.Radiobutton(mode_frame, text="Uniformity", variable=self.view_mode, value="Uniformity", command=self.load_image_list).pack(side=tk.LEFT, expand=True)


        sort_frame = ttk.LabelFrame(list_frame, text="Sort By", padding=5)
        sort_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(sort_frame, text="Name (A-Z)", command=lambda: self.set_sort_mode_and_update('name')).pack(side=tk.LEFT, expand=True, padx=2)
        ttk.Button(sort_frame, text="Time (Newest)", command=lambda: self.set_sort_mode_and_update('time')).pack(side=tk.LEFT, expand=True, padx=2)
        
        self.listbox = Listbox(list_frame, selectmode=tk.EXTENDED)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        action_frame = ttk.Frame(list_frame)
        action_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        delete_button = ttk.Button(action_frame, text="Delete Selected Image(s) 🗑️", command=self.delete_selected_images)
        delete_button.pack(fill=tk.X, expand=True)

        canvas_container = ttk.Frame(paned_window)
        paned_window.add(canvas_container, weight=4)

        self.v_scrollbar = ttk.Scrollbar(canvas_container, orient=tk.VERTICAL)
        self.h_scrollbar = ttk.Scrollbar(canvas_container, orient=tk.HORIZONTAL)

        self.canvas = tk.Canvas(
            canvas_container, bg="gray",
            yscrollcommand=self.v_scrollbar.set,
            xscrollcommand=self.h_scrollbar.set
        )
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.v_scrollbar.config(command=self.canvas.yview)
        self.v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.h_scrollbar.config(command=self.canvas.xview)
        self.h_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)
        
        zoom_frame = ttk.Frame(canvas_container, padding=5)
        zoom_frame.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(zoom_frame, text="Zoom In (+)", command=self.zoom_in).pack(side=tk.LEFT, expand=True, padx=2)
        ttk.Button(zoom_frame, text="Zoom Out (-)", command=self.zoom_out).pack(side=tk.LEFT, expand=True, padx=2)
        ttk.Button(zoom_frame, text="Fit to Screen", command=self.fit_to_screen).pack(side=tk.LEFT, expand=True, padx=2)
        # --- [수정됨] 새로고침 버튼 추가 ---
        ttk.Button(zoom_frame, text="Refresh 🔄", command=self.load_image_list).pack(side=tk.LEFT, expand=True, padx=2)


        self.listbox.bind("<<ListboxSelect>>", self.on_listbox_select)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", self.on_mouse_wheel)
        self.canvas.bind("<Button-5>", self.on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press_for_pan)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag_for_pan)


        self.load_image_list()

    def zoom_in(self):
        self.zoom_factor *= 1.1
        self.show_image()

    def zoom_out(self):
        self.zoom_factor /= 1.1
        self.show_image()

    def fit_to_screen(self):
        self.zoom_factor = self._calculate_fit_zoom()
        self.show_image()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def on_canvas_press_for_pan(self, event):
        self._x = event.x
        self._y = event.y

    def on_canvas_drag_for_pan(self, event):
        # 민감도 변수 self.pan_sensitivity를 사용하여 이동량 조절
        self.canvas.xview_scroll(int((self._x - event.x) * self.pan_sensitivity), "units")
        self.canvas.yview_scroll(int((self._y - event.y) * self.pan_sensitivity), "units")
        self._x = event.x
        self._y = event.y


    def delete_selected_images(self, *args):
        selected_indices = self.listbox.curselection()
        if not selected_indices:
            messagebox.showwarning("Warning", "Please select image(s) to delete.")
            return
        
        num_selected = len(selected_indices)
        confirmed = messagebox.askyesno(
            "Confirm Deletion",
            f"Are you sure you want to permanently delete {num_selected} selected image(s)?\n\nThis action cannot be undone."
        )

        if confirmed:
            try:
                for index in sorted(selected_indices, reverse=True):
                    file_to_delete = self.image_paths[index]
                    os.remove(file_to_delete)
                    self.image_paths.pop(index)
                    self.listbox.delete(index)
                self.canvas.delete("all")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to delete file(s):\n{e}")

    def set_sort_mode_and_update(self, mode):
        self.sort_mode.set(mode)
        self.sort_and_display_images()

    def sort_and_display_images(self):
        sort_by = self.sort_mode.get()
        if sort_by == 'name':
            self.image_paths.sort(key=lambda p: os.path.basename(p))
        elif sort_by == 'time':
            self.image_paths.sort(key=os.path.getmtime, reverse=True)

        self.listbox.delete(0, tk.END)
        show_prefix = self.view_mode.get() == "All"
        for path in self.image_paths:
            display_name = os.path.basename(path)
            if show_prefix:
                parent_dir = os.path.basename(os.path.dirname(path))
                if parent_dir in ["ByProduce", "ByAnalysis", "Contour", "Uniformity"]:
                    display_name = f"{parent_dir}/{display_name}"
            self.listbox.insert(tk.END, display_name)

    def load_image_list(self):
        self.image_paths.clear()
        self.canvas.delete("all")

        try:
            if not os.path.isdir(self.base_image_dir):
                raise FileNotFoundError

            mode = self.view_mode.get()
            dirs_to_scan = []
            
            if mode == "All":
                dirs_to_scan.extend([
                    os.path.join(self.base_image_dir, 'ByProduce'),
                    os.path.join(self.base_image_dir, 'ByAnalysis'),
                    os.path.join(self.base_image_dir, 'Contour'),
                    os.path.join(self.base_image_dir, 'Uniformity')
                ])
            else:
                dirs_to_scan.append(os.path.join(self.base_image_dir, mode))

            valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.bmp')
            
            for dir_path in dirs_to_scan:
                if os.path.isdir(dir_path):
                    for f in os.listdir(dir_path):
                        if f.lower().endswith(valid_extensions):
                            self.image_paths.append(os.path.join(dir_path, f))
            
            self.sort_and_display_images()

            if not self.image_paths:
                self.listbox.insert(tk.END, "No images found in this mode.")

        except FileNotFoundError:
            messagebox.showerror("Error", f"Base image directory not found: {self.base_image_dir}")
            self.destroy()

    def _calculate_fit_zoom(self):
        if not self.pil_image: return 1.0
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        if canvas_width <= 1 or canvas_height <= 1: return 1.0
        img_width, img_height = self.pil_image.size
        width_ratio = canvas_width / img_width
        height_ratio = canvas_height / img_height
        fit_zoom = min(width_ratio, height_ratio) * 0.95
        return fit_zoom if fit_zoom > 0 else 1.0

    def on_listbox_select(self, event):
        selected_indices = self.listbox.curselection()
        if not selected_indices: return
        
        index = selected_indices[0]
        if not self.image_paths or index >= len(self.image_paths):
            return

        image_path = self.image_paths[index]
        try:
            self.pil_image = Image.open(image_path)
            self.zoom_factor = self._calculate_fit_zoom()
            self.show_image()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open image:\n{e}")

    def show_image(self):
        if not self.pil_image: return
        width = int(self.pil_image.width * self.zoom_factor)
        height = int(self.pil_image.height * self.zoom_factor)
        if width < 1 or height < 1: return
        resized_image = self.pil_image.resize((width, height), Image.Resampling.LANCZOS)
        # *** 여기가 수정된 부분입니다 (master=self.canvas 추가) ***
        self.tk_image = ImageTk.PhotoImage(resized_image, master=self.canvas)
        
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_image)
        self.canvas.config(scrollregion=(0, 0, width, height))

    def on_mouse_wheel(self, event):
        if hasattr(event, 'delta') and event.delta > 0 or getattr(event, 'num', 0) == 4:
            self.zoom_in()
        elif hasattr(event, 'delta') and event.delta < 0 or getattr(event, 'num', 0) == 5:
            self.zoom_out()
