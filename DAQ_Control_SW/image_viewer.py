import tkinter as tk
from tkinter import ttk, Toplevel, messagebox, Listbox, filedialog
import os
from datetime import datetime

# 이미지 처리를 위한 Pillow 임포트
try:
    from PIL import Image, ImageTk
except ImportError:
    messagebox.showerror("Error", "Pillow library not found. Please run 'pip install Pillow'")

# PDF 지원을 위한 라이브러리 임포트 및 체크
try:
    from pdf2image import convert_from_path
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    # print("Warning: 'pdf2image' not installed. PDF viewing will be disabled.")
except Exception as e:
    PDF_SUPPORT = False
    # print(f"Warning: PDF support disabled: {e}. (Poppler installation may be required)")

class ImageViewer(Toplevel):
    def __init__(self, master, config_manager):
        super().__init__(master)
        self.title("Image & PDF Viewer")
        self.geometry("1600x900")

        # 설정에서 이미지 경로 가져오기
        self.base_image_dir = config_manager.get_config_value("ImagePath")
        if not self.base_image_dir:
            messagebox.showerror("Error", "ImagePath not found in config file.")
            self.destroy()
            return

        # 데이터 변수 초기화
        self.full_image_paths = [] # 원본 전체 경로 목록
        self.display_paths = []    # 필터링/정렬된 현재 표시 목록
        self.pil_image = None
        self.tk_image = None
        self.zoom_factor = 1.0
        self.view_mode = tk.StringVar(value="All")
        self.sort_mode = tk.StringVar(value="name")
        self.search_var = tk.StringVar()
        
        # 캔버스 크기 변경 감지용 타이머
        self.resize_timer = None

        # 레이아웃 구성
        paned_window = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True)

        # --- 좌측 사이드바 (컨트롤 패널) ---
        list_frame = ttk.Frame(paned_window, width=400)
        paned_window.add(list_frame, weight=1)

        # 1. 뷰 모드 선택
        mode_frame = ttk.LabelFrame(list_frame, text="View Mode", padding=5)
        mode_frame.pack(fill=tk.X, padx=5, pady=5)
        modes = ["All", "ByProduce", "ByAnalysis", "Contour", "Uniformity", "Noise"]
        for m in modes:
            ttk.Radiobutton(mode_frame, text=m.replace("By", ""), variable=self.view_mode, 
                            value=m, command=self.load_image_list).pack(side=tk.LEFT, expand=True)

        # 2. 검색창
        search_frame = ttk.LabelFrame(list_frame, text="Search (File Name)", padding=5)
        search_frame.pack(fill=tk.X, padx=5, pady=5)
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(fill=tk.X, padx=5, pady=2)
        self.search_var.trace_add("write", lambda *args: self.filter_images())

        # 3. 정렬 버튼
        sort_btn_frame = ttk.Frame(list_frame)
        sort_btn_frame.pack(fill=tk.X, padx=5)
        ttk.Button(sort_btn_frame, text="Sort by Name", command=lambda: self.set_sort_mode('name')).pack(side=tk.LEFT, expand=True)
        ttk.Button(sort_btn_frame, text="Sort by Time", command=lambda: self.set_sort_mode('time')).pack(side=tk.LEFT, expand=True)

        # 4. 목록창 (스크롤바 포함)
        list_container = ttk.Frame(list_frame)
        list_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL)
        # --- [수정] 폰트 크기 10 -> 12로 증가 ---
        self.listbox = Listbox(list_container, selectmode=tk.EXTENDED, 
                               yscrollcommand=self.scrollbar.set, font=("Helvetica", 12))
        self.scrollbar.config(command=self.listbox.yview)
        
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 5. 하단 액션 버튼
        action_frame = ttk.Frame(list_frame)
        action_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(action_frame, text="Convert Selected to PDF 📄", command=self.convert_to_pdf).pack(fill=tk.X, pady=2)
        ttk.Button(action_frame, text="Delete Selected 🗑️", command=self.delete_selected).pack(fill=tk.X, pady=2)

        # --- 우측 메인 패널 (이미지 뷰어) ---
        viewer_frame = ttk.Frame(paned_window)
        paned_window.add(viewer_frame, weight=4)

        # 캔버스 생성 (배경색 지정)
        self.canvas = tk.Canvas(viewer_frame, bg="#2b2b2b", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 줌 컨트롤 바
        zoom_bar = ttk.Frame(viewer_frame, padding=5)
        zoom_bar.pack(fill=tk.X)
        ttk.Button(zoom_bar, text="Zoom In (+)", command=self.zoom_in).pack(side=tk.LEFT, padx=5)
        ttk.Button(zoom_bar, text="Zoom Out (-)", command=self.zoom_out).pack(side=tk.LEFT, padx=5)
        ttk.Button(zoom_bar, text="Center Fit", command=self.fit_to_screen).pack(side=tk.LEFT, padx=5)
        self.info_label = ttk.Label(zoom_bar, text="Zoom: 100%")
        self.info_label.pack(side=tk.RIGHT, padx=10)

        # 이벤트 바인딩
        self.listbox.bind("<<ListboxSelect>>", self.on_listbox_select)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        
        # --- [추가] 이미지 이동(Pan)을 위한 마우스 이벤트 바인딩 ---
        self.canvas.bind("<ButtonPress-1>", self.pan_start)
        self.canvas.bind("<B1-Motion>", self.pan_move)
        # 드래그 가능함을 나타내는 커서 설정
        self.canvas.config(cursor="hand2")

        # 데이터 로드
        self.load_image_list()

    # --- [추가] 이미지 이동 시작 ---
    def pan_start(self, event):
        """마우스 클릭 시 이동 시작점 기록"""
        self.canvas.scan_mark(event.x, event.y)

    # --- [추가] 이미지 이동 중 ---
    def pan_move(self, event):
        """마우스 드래그 시 캔버스 뷰 이동"""
        # gain 값이 클수록 더 빠르게 이동합니다.
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def load_image_list(self):
        """파일 시스템에서 이미지 및 PDF 목록을 불러옴"""
        self.full_image_paths.clear()
        mode = self.view_mode.get()
        
        # 허용 확장자 설정
        valid_ext = ['.png', '.jpg', '.jpeg']
        if PDF_SUPPORT:
            valid_ext.append('.pdf')
        valid_ext = tuple(valid_ext)

        # 대상 디렉토리 결정
        sub_dirs = ["ByProduce", "ByAnalysis", "Contour", "Uniformity", "Noise"]
        target_dirs = [os.path.join(self.base_image_dir, d) for d in (sub_dirs if mode == "All" else [mode])]

        for d in target_dirs:
            if os.path.exists(d):
                for f in os.listdir(d):
                    if f.lower().endswith(valid_ext):
                        self.full_image_paths.append(os.path.join(d, f))
        
        self.sort_images()
        self.filter_images()

    def sort_images(self):
        """설정된 정렬 모드에 따라 목록 정렬"""
        if self.sort_mode.get() == 'name':
            self.full_image_paths.sort(key=lambda x: os.path.basename(x).lower())
        else:
            self.full_image_paths.sort(key=os.path.getmtime, reverse=True)

    def set_sort_mode(self, mode):
        self.sort_mode.set(mode)
        self.sort_images()
        self.filter_images()

    def filter_images(self):
        """검색어에 따라 목록 필터링 후 표시"""
        query = self.search_var.get().lower()
        self.display_paths = [p for p in self.full_image_paths if query in os.path.basename(p).lower()]
        
        self.listbox.delete(0, tk.END)
        for p in self.display_paths:
            name = os.path.basename(p)
            if self.view_mode.get() == "All":
                category = os.path.basename(os.path.dirname(p))
                name = f"[{category}] {name}"
            self.listbox.insert(tk.END, name)

    def on_listbox_select(self, event):
        """목록 선택 시 이미지/PDF 로드"""
        idx = self.listbox.curselection()
        if not idx: return
        
        path = self.display_paths[idx[0]]
        try:
            if path.lower().endswith('.pdf') and PDF_SUPPORT:
                # PDF의 첫 페이지만 이미지로 변환하여 로드
                pages = convert_from_path(path, first_page=1, last_page=1)
                self.pil_image = pages[0]
            else:
                self.pil_image = Image.open(path)
            
            # 새 이미지 로드 시 뷰 위치 초기화
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
            self.fit_to_screen()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load file:\n{e}")

    def on_canvas_resize(self, event):
        """창 크기가 변할 때 이미지를 화면에 맞춤 (약간의 딜레이)"""
        if self.resize_timer:
            self.after_cancel(self.resize_timer)
        self.resize_timer = self.after(100, self.fit_to_screen)

    def fit_to_screen(self):
        """이미지를 캔버스 중앙에 비율 유지하며 가득 채우기"""
        if not self.pil_image: return
        
        # 캔버스 크기 가져오기
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1: cw, ch = 1200, 700 # 초기값 대응

        # 이미지 크기 가져오기
        iw, ih = self.pil_image.size
        
        # 최적 비율 계산
        self.zoom_factor = min(cw / iw, ch / ih)
        self.show_image()

    def show_image(self):
        """캔버스에 최종 이미지 렌더링"""
        if not self.pil_image: return
        
        # 현재 줌 비율에 맞춰 리사이즈
        nw = int(self.pil_image.width * self.zoom_factor)
        nh = int(self.pil_image.height * self.zoom_factor)
        
        if nw < 1 or nh < 1: return
        
        resized = self.pil_image.resize((nw, nh), Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(resized, master=self.canvas)
        
        self.canvas.delete("all")
        # 중앙 배치 (앵커를 중앙으로 설정)
        self.canvas.create_image(self.canvas.winfo_width()//2, self.canvas.winfo_height()//2, 
                                 anchor=tk.CENTER, image=self.tk_image)
        
        self.info_label.config(text=f"Zoom: {int(self.zoom_factor * 100)}%")

    def zoom_in(self):
        self.zoom_factor *= 1.2
        self.show_image()

    def zoom_out(self):
        self.zoom_factor /= 1.2
        self.show_image()

    def convert_to_pdf(self):
        """선택된 여러 이미지들을 하나의 PDF로 합쳐 저장"""
        indices = self.listbox.curselection()
        if not indices:
            messagebox.showwarning("Warning", "Please select images from the list first.")
            return

        selected_files = [self.display_paths[i] for i in indices if not self.display_paths[i].lower().endswith('.pdf')]
        if not selected_files:
            messagebox.showwarning("Warning", "No images selected (PDFs are excluded from merge).")
            return

        # 기본 파일명 제안: Report_YYYYMMDD_HHMM.pdf
        default_name = f"Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        save_path = filedialog.asksaveasfilename(defaultextension=".pdf", 
                                                 filetypes=[("PDF files", "*.pdf")],
                                                 initialfile=default_name)
        if not save_path: return

        try:
            image_list = []
            for f in selected_files:
                img = Image.open(f).convert('RGB')
                image_list.append(img)
            
            if image_list:
                image_list[0].save(save_path, save_all=True, append_images=image_list[1:])
                messagebox.showinfo("Success", f"PDF created successfully:\n{os.path.basename(save_path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to create PDF:\n{e}")

    def delete_selected(self):
        """선택된 파일 삭제"""
        indices = self.listbox.curselection()
        if not indices: return
        
        if messagebox.askyesno("Confirm", f"Are you sure you want to delete {len(indices)} files?"):
            for i in sorted(indices, reverse=True):
                path = self.display_paths[i]
                try:
                    os.remove(path)
                    self.full_image_paths.remove(path)
                except Exception as e:
                    print(f"Error deleting {path}: {e}")
            self.filter_images()
            self.canvas.delete("all")
            self.pil_image = None
