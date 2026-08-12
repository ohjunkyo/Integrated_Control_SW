import tkinter as tk
from tkinter import messagebox, Listbox, filedialog
import os
import time
import threading
from datetime import datetime

import customtkinter as ctk

# 이미지 처리를 위한 Pillow 임포트
try:
    from PIL import Image, ImageTk
except ImportError:
    messagebox.showerror("Error", "Pillow library not found. Please run 'pip install Pillow'")

# PDF 렌더링: PyMuPDF(fitz). 인프로세스 C 렌더러라 pdf2image(Poppler subprocess)
# 보다 훨씬 빠르고, 썸네일도 싸게 뽑을 수 있어 사이드바 표시에 쓴다.
try:
    import fitz  # PyMuPDF
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False


# ── 팔레트 (앱 전역 light 테마와 통일; 뷰 캔버스만 다크) ──────────────────
COL_BG        = "#f2f3f5"   # 창 배경
COL_CARD      = "#ffffff"   # 사이드바/툴바 카드
COL_CANVAS    = "#232629"   # 이미지/PDF 뷰 캔버스 (다크)
COL_THUMB_BG  = "#1e2124"   # 썸네일 스트립 배경
COL_ACCENT    = "#0a84ff"   # 단일 액센트 (파랑)
COL_ACCENT_HV = "#0060df"
COL_GREEN     = "#2e9e4f"
COL_GREEN_HV  = "#268043"
COL_GRAY      = "#6c757d"
COL_GRAY_HV   = "#5a6268"
COL_TEXT_MUTE = "#8a9099"


class ImageViewer(ctk.CTkToplevel):
    # PDF 페이지 렌더 기본 배율. 화면 fit 후 확대해도 어느 정도 선명하도록
    # 2배(=144dpi 상당)로 렌더해 캐시하고, 줌은 이 이미지를 리사이즈한다.
    PDF_BASE_ZOOM = 2.0
    THUMB_ZOOM = 0.18  # 썸네일 렌더 배율
    WHEEL_COOLDOWN = 0.25  # 휠 페이지 넘김 최소 간격(초) — 과민 방지

    def __init__(self, master, config_manager):
        super().__init__(master)
        # 앱 나머지와 동일한 light/blue 테마 유지 (전역 모드 변경 없음).
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.title("Image & PDF Viewer")
        self.geometry("1600x900")
        self.configure(fg_color=COL_BG)

        self.base_image_dir = config_manager.get_config_value("ImagePath")
        if not self.base_image_dir:
            messagebox.showerror("Error", "ImagePath not found in config file.")
            self.destroy()
            return

        # ── 상태 변수 ──
        self.full_image_paths = []
        self.display_paths = []
        self.pil_image = None
        self.tk_image = None
        self.zoom_factor = 1.0

        self.pdf_doc = None
        self.current_pdf_path = None
        self.current_pdf_page = 1
        self.pdf_page_count = 1
        self._page_cache = {}
        self._thumb_images = []
        self._thumb_widgets = {}
        self._thumb_job = 0
        self._thumb_shown = False
        self._last_wheel_flip = 0.0

        self.view_mode = tk.StringVar(value="All")
        self.sort_mode = tk.StringVar(value="time")   # default newest-first (sort_images() below reverses time sort)
        self.search_var = tk.StringVar()
        self.resize_timer = None

        # ── 3-컬럼 그리드: [사이드바 | 썸네일 | 뷰어] ──
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(2, weight=1)

        self._build_sidebar()
        self._build_thumb_panel()
        self._build_viewer()

        self.bind("<Prior>", lambda e: self.prev_pdf_page())   # PageUp
        self.bind("<Next>", lambda e: self.next_pdf_page())    # PageDown
        self.bind("<Key-plus>", lambda e: self.zoom_in())
        self.bind("<Key-equal>", lambda e: self.zoom_in())
        self.bind("<Key-minus>", lambda e: self.zoom_out())
        self.bind("<Key-f>", lambda e: self.fit_to_screen())

        self.load_image_list()

    # ═══════════════════════════════════════════════════════ UI 빌드
    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, width=470, corner_radius=0, fg_color=COL_CARD)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.grid_propagate(False)
        bar.grid_rowconfigure(4, weight=1)
        bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(bar, text="Files", font=ctk.CTkFont(size=20, weight="bold"),
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))

        # 뷰 모드: 라디오 버튼 한 줄 (기존 UX 그대로 -- 드롭다운보다 클릭
        # 한 번에 바로 전환되는 게 편하다는 피드백에 따라 되돌림).
        # NOTE: grid + equal weight columns를 썼더니 CTkRadioButton은 ttk와
        # 달리 렌더링 폭이 고정돼 있어, 칸이 좁아지면 텍스트가 줄어들지
        # 않고 그냥 잘려나갔다 ("Uniformity" -> "Uni"). pack(expand=True)로
        # 되돌리면 각 버튼이 필요한 만큼만 자리를 쓰고 나머지 여백을
        # 나눠 가져서 잘리지 않는다 (원래 ttk 버전과 동일한 방식).
        # 세로 배치: 한 줄에 6개를 넣으면 CTkRadioButton 이 텍스트를 줄이지
        # 못하고 잘려서(Uniformity -> "U"...), 폭은 그대로 두고 세로로 쌓는다.
        # 2열 그리드로 세로 공간을 아끼면서 전 라벨이 온전히 보이게 한다.
        mode_frame = ctk.CTkFrame(bar, corner_radius=10, fg_color="#f6f7f9")
        mode_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        mode_frame.grid_columnconfigure((0, 1), weight=1)
        modes = ["All", "ByProduce", "ByAnalysis", "Contour", "Uniformity", "Rate"]
        for i, m in enumerate(modes):
            ctk.CTkRadioButton(
                mode_frame, text=m.replace("By", ""), variable=self.view_mode, value=m,
                command=self.load_image_list, fg_color=COL_ACCENT, hover_color=COL_ACCENT_HV
            ).grid(row=i // 2, column=i % 2, sticky="w", padx=14, pady=7)

        # 검색창
        self.search_entry = ctk.CTkEntry(bar, textvariable=self.search_var,
                                         placeholder_text="🔍  Search file name…", height=36)
        self.search_entry.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 10))
        self.search_var.trace_add("write", lambda *a: self.filter_images())

        # 정렬 세그먼트 + 새로고침
        sort_row = ctk.CTkFrame(bar, fg_color="transparent")
        sort_row.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 10))
        sort_row.grid_columnconfigure(0, weight=1)
        self.sort_seg = ctk.CTkSegmentedButton(
            sort_row, values=["Name", "Time"], command=self._on_sort_change,
            selected_color=COL_ACCENT, selected_hover_color=COL_ACCENT_HV)
        self.sort_seg.set("Time")
        self.sort_seg.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(sort_row, text="🔄", width=40, command=self.load_image_list,
                      fg_color=COL_GRAY, hover_color=COL_GRAY_HV).grid(row=0, column=1, padx=(8, 0))

        # 파일 목록 — 성능상 tk.Listbox 유지하되 플랫/모던하게 스타일
        list_holder = ctk.CTkFrame(bar, corner_radius=10, fg_color="#f6f7f9")
        list_holder.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 10))
        list_holder.grid_rowconfigure(0, weight=1)
        list_holder.grid_columnconfigure(0, weight=1)
        self.scrollbar = ctk.CTkScrollbar(list_holder)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)
        self.listbox = Listbox(
            list_holder, selectmode=tk.EXTENDED, activestyle="none",
            bg="#f6f7f9", fg="#2d2d30", borderwidth=0, highlightthickness=0,
            selectbackground=COL_ACCENT, selectforeground="white",
            font=("Helvetica", 12), yscrollcommand=self.scrollbar.set)
        self.scrollbar.configure(command=self.listbox.yview)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=4)
        self.listbox.bind("<<ListboxSelect>>", self.on_listbox_select)

        # 하단 액션
        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 16))
        actions.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(actions, text="📄  Convert Selected to PDF", height=38,
                      fg_color=COL_GREEN, hover_color=COL_GREEN_HV,
                      command=self.convert_to_pdf).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkButton(actions, text="🗑  Delete Selected", height=38,
                      fg_color="#c0392b", hover_color="#a93226",
                      command=self.delete_selected).grid(row=1, column=0, sticky="ew")

    def _build_thumb_panel(self):
        self.thumb_frame = ctk.CTkFrame(self, width=180, corner_radius=0, fg_color=COL_THUMB_BG)
        self.thumb_frame.grid(row=0, column=1, sticky="ns")
        self.thumb_frame.grid_propagate(False)
        self.thumb_frame.grid_rowconfigure(1, weight=1)
        self.thumb_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.thumb_frame, text="PAGES", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#cfd3d8").grid(row=0, column=0, sticky="ew", pady=(10, 4))

        body = tk.Frame(self.thumb_frame, bg=COL_THUMB_BG)
        body.grid(row=1, column=0, sticky="nsew")
        self.thumb_canvas = tk.Canvas(body, width=150, bg=COL_THUMB_BG, highlightthickness=0)
        tsc = ctk.CTkScrollbar(body, command=self.thumb_canvas.yview)
        self.thumb_canvas.configure(yscrollcommand=tsc.set)
        tsc.pack(side=tk.RIGHT, fill=tk.Y)
        self.thumb_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.thumb_inner = tk.Frame(self.thumb_canvas, bg=COL_THUMB_BG)
        self._thumb_win = self.thumb_canvas.create_window((0, 0), window=self.thumb_inner, anchor="nw")
        self.thumb_inner.bind(
            "<Configure>", lambda e: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all")))
        # Bind the wheel directly to the canvas AND the inner frame -- once
        # thumbnails are added they're child Labels stacked on top of the
        # canvas, so hovering over an actual thumbnail fires Enter/Leave on
        # the Label, not the canvas, and a bind_all-on-Enter approach (as used
        # here before) silently stops working as soon as the sidebar fills up.
        # Direct per-widget binding has no such blind spot; every new
        # thumbnail widget gets the same binding in _add_thumbnail below.
        self._bind_thumb_wheel(self.thumb_canvas)
        self._bind_thumb_wheel(self.thumb_inner)

        # PDF 아닐 때는 통째로 숨긴다 (요청: pdf 일 때만 표시).
        self.thumb_frame.grid_remove()

    def _build_viewer(self):
        viewer = ctk.CTkFrame(self, corner_radius=0, fg_color=COL_BG)
        viewer.grid(row=0, column=2, sticky="nsew")
        viewer.grid_rowconfigure(0, weight=1)
        viewer.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(viewer, bg=COL_CANVAS, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 6))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>", self.pan_start)
        self.canvas.bind("<B1-Motion>", self.pan_move)
        self.canvas.config(cursor="hand2")
        self.canvas.bind("<MouseWheel>", self._on_view_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_view_wheel(e, +1))
        self.canvas.bind("<Button-5>", lambda e: self._on_view_wheel(e, -1))

        # 하단 툴바 (아이콘 버튼 압축형)
        tb = ctk.CTkFrame(viewer, corner_radius=12, fg_color=COL_CARD)
        tb.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

        def ibtn(parent, txt, cmd, w=44):
            return ctk.CTkButton(parent, text=txt, width=w, height=34, command=cmd,
                                 fg_color=COL_GRAY, hover_color=COL_GRAY_HV,
                                 font=ctk.CTkFont(size=15))

        left = ctk.CTkFrame(tb, fg_color="transparent")
        left.pack(side=tk.LEFT, padx=8, pady=8)
        ibtn(left, "➖", self.zoom_out).pack(side=tk.LEFT, padx=3)
        ibtn(left, "➕", self.zoom_in).pack(side=tk.LEFT, padx=3)
        ibtn(left, "⤢  Fit", self.fit_to_screen, w=72).pack(side=tk.LEFT, padx=3)

        mid = ctk.CTkFrame(tb, fg_color="transparent")
        mid.pack(side=tk.LEFT, padx=20, pady=8)
        self.prev_page_btn = ibtn(mid, "◀", self.prev_pdf_page)
        self.prev_page_btn.pack(side=tk.LEFT, padx=3)
        self.page_label = ctk.CTkLabel(mid, text="", font=ctk.CTkFont(size=13, weight="bold"), width=110)
        self.page_label.pack(side=tk.LEFT, padx=6)
        self.next_page_btn = ibtn(mid, "▶", self.next_pdf_page)
        self.next_page_btn.pack(side=tk.LEFT, padx=3)

        self.info_label = ctk.CTkLabel(tb, text="Zoom: 100%", text_color=COL_TEXT_MUTE,
                                       font=ctk.CTkFont(size=13))
        self.info_label.pack(side=tk.RIGHT, padx=16, pady=8)
        self.update_page_controls()

    # ═══════════════════════════════════════════════════════ 컨트롤 콜백
    def _on_sort_change(self, value):
        self.set_sort_mode('name' if value == "Name" else 'time')

    # ───────────────────────────────────────────────────────── Pan / Wheel
    def pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def pan_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _bind_thumb_wheel(self, widget):
        """썸네일 사이드바 위젯에 휠 스크롤을 직접 건다 (bind_all 방식의
        Enter/Leave 사각지대 없이, 위젯 하나하나에 바로)."""
        widget.bind("<MouseWheel>", self._on_thumb_wheel)          # Win/Mac
        widget.bind("<Button-4>", lambda e: self.thumb_canvas.yview_scroll(-1, "units"))  # Linux up
        widget.bind("<Button-5>", lambda e: self.thumb_canvas.yview_scroll(1, "units"))   # Linux down

    def _on_thumb_wheel(self, event):
        # 페이지 넘김(쿨다운 있음)과 달리, 좌측 파일 목록과 동일한 감도의
        # 일반 스크롤 -- 쓰로틀 없이 노치당 1 unit씩 즉시 반응한다.
        self.thumb_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _on_view_wheel(self, event, direction=None):
        """메인 뷰어에서 휠을 굴리면 PDF 페이지를 넘긴다. PDF 가 아니면 무시.
        쿨다운을 둬서 한 번 굴릴 때 페이지가 우르르 넘어가지 않게 한다."""
        if not self.pdf_doc:
            return
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        now = time.time()
        if now - self._last_wheel_flip < self.WHEEL_COOLDOWN:
            return
        self._last_wheel_flip = now
        if direction < 0:
            self.next_pdf_page()
        else:
            self.prev_pdf_page()

    def _show_thumb_panel(self):
        if not self._thumb_shown:
            self.thumb_frame.grid()
            self._thumb_shown = True

    def _hide_thumb_panel(self):
        if self._thumb_shown:
            self.thumb_frame.grid_remove()
            self._thumb_shown = False

    # ═══════════════════════════════════════════════════════ 파일 목록
    def load_image_list(self):
        self.full_image_paths.clear()
        mode = self.view_mode.get()
        valid_ext = ['.png', '.jpg', '.jpeg']
        if PDF_SUPPORT:
            valid_ext.append('.pdf')
        valid_ext = tuple(valid_ext)

        sub_dirs = ["ByProduce", "ByAnalysis", "Contour", "Uniformity", "Rate"]
        target_dirs = [os.path.join(self.base_image_dir, d) for d in (sub_dirs if mode == "All" else [mode])]
        for d in target_dirs:
            if os.path.exists(d):
                for f in os.listdir(d):
                    if f.lower().endswith(valid_ext):
                        self.full_image_paths.append(os.path.join(d, f))
        self.sort_images()
        self.filter_images()

    def sort_images(self):
        if self.sort_mode.get() == 'name':
            self.full_image_paths.sort(key=lambda x: os.path.basename(x).lower())
        else:
            self.full_image_paths.sort(key=os.path.getmtime, reverse=True)

    def set_sort_mode(self, mode):
        self.sort_mode.set(mode)
        self.sort_images()
        self.filter_images()

    def filter_images(self):
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
        idx = self.listbox.curselection()
        if not idx:
            return
        path = self.display_paths[idx[0]]
        try:
            if path.lower().endswith('.pdf') and PDF_SUPPORT:
                self.open_pdf(path)
            else:
                self._close_pdf()
                self.pil_image = Image.open(path)
                self.canvas.xview_moveto(0)
                self.canvas.yview_moveto(0)
                self.fit_to_screen()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load file:\n{e}")

    # ═══════════════════════════════════════════════════════ PDF 코어
    def open_pdf(self, path):
        self._close_pdf()
        self.pdf_doc = fitz.open(path)
        self.current_pdf_path = path
        self.pdf_page_count = self.pdf_doc.page_count
        self._page_cache = {}
        self._show_thumb_panel()
        self.load_pdf_page(1)
        self._build_thumbnails()

    def _close_pdf(self):
        self._thumb_job += 1
        if self.pdf_doc is not None:
            try:
                self.pdf_doc.close()
            except Exception:
                pass
        self.pdf_doc = None
        self.current_pdf_path = None
        self.pdf_page_count = 1
        self.current_pdf_page = 1
        self._page_cache = {}
        self.update_page_controls()
        self._clear_thumbnails()
        self._hide_thumb_panel()

    def _render_pdf_page(self, page_num, zoom):
        page = self.pdf_doc[page_num - 1]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def load_pdf_page(self, page_num):
        if not self.pdf_doc:
            return
        page_num = max(1, min(page_num, self.pdf_page_count))
        try:
            img = self._page_cache.get(page_num)
            if img is None:
                img = self._render_pdf_page(page_num, self.PDF_BASE_ZOOM)
                self._page_cache[page_num] = img
            self.pil_image = img
            self.current_pdf_page = page_num
            self.update_page_controls()
            self._highlight_thumbnail(page_num)
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
            self.fit_to_screen()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load PDF page:\n{e}")

    def prev_pdf_page(self):
        if self.pdf_doc and self.current_pdf_page > 1:
            self.load_pdf_page(self.current_pdf_page - 1)

    def next_pdf_page(self):
        if self.pdf_doc and self.current_pdf_page < self.pdf_page_count:
            self.load_pdf_page(self.current_pdf_page + 1)

    def update_page_controls(self):
        if self.pdf_doc:
            self.page_label.configure(text=f"Page {self.current_pdf_page} / {self.pdf_page_count}")
            self.prev_page_btn.configure(state="normal" if self.current_pdf_page > 1 else "disabled")
            self.next_page_btn.configure(state="normal" if self.current_pdf_page < self.pdf_page_count else "disabled")
        else:
            self.page_label.configure(text="")
            self.prev_page_btn.configure(state="disabled")
            self.next_page_btn.configure(state="disabled")

    # ═══════════════════════════════════════════════════════ 썸네일
    def _clear_thumbnails(self):
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        self._thumb_images = []
        self._thumb_widgets = {}

    def _build_thumbnails(self):
        self._clear_thumbnails()
        self._thumb_job += 1
        job = self._thumb_job
        path = self.current_pdf_path
        n = self.pdf_page_count

        def worker():
            try:
                doc = fitz.open(path)
            except Exception:
                return
            mat = fitz.Matrix(self.THUMB_ZOOM, self.THUMB_ZOOM)
            for i in range(1, n + 1):
                if job != self._thumb_job:
                    break
                try:
                    pix = doc[i - 1].get_pixmap(matrix=mat, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                except Exception:
                    continue
                self.after(0, self._add_thumbnail, job, i, img)
            try:
                doc.close()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _add_thumbnail(self, job, page_num, pil_img):
        if job != self._thumb_job:
            return
        try:
            photo = ImageTk.PhotoImage(pil_img, master=self.thumb_canvas)
        except Exception:
            return
        self._thumb_images.append(photo)

        holder = tk.Frame(self.thumb_inner, bg=COL_THUMB_BG, padx=3, pady=3,
                          highlightthickness=2, highlightbackground=COL_THUMB_BG)
        holder.pack(pady=4)
        lbl = tk.Label(holder, image=photo, bg=COL_THUMB_BG, cursor="hand2")
        lbl.pack()
        num = tk.Label(holder, text=str(page_num), fg="#cfd3d8", bg=COL_THUMB_BG,
                       font=("Helvetica", 8))
        num.pack()
        for w in (lbl, num, holder):
            w.bind("<Button-1>", lambda e, p=page_num: self.load_pdf_page(p))
            self._bind_thumb_wheel(w)
        self._thumb_widgets[page_num] = holder
        if page_num == self.current_pdf_page:
            self._highlight_thumbnail(page_num)

    def _highlight_thumbnail(self, page_num):
        for p, holder in self._thumb_widgets.items():
            holder.config(highlightbackground=COL_ACCENT if p == page_num else COL_THUMB_BG)
        holder = self._thumb_widgets.get(page_num)
        if holder is not None:
            self.thumb_canvas.update_idletasks()
            try:
                total = self.thumb_inner.winfo_height()
                y = holder.winfo_y()
                if total > 0:
                    self.thumb_canvas.yview_moveto(max(0.0, (y - 40) / total))
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════ 렌더링
    def on_canvas_resize(self, event):
        if self.resize_timer:
            self.after_cancel(self.resize_timer)
        self.resize_timer = self.after(100, self.fit_to_screen)

    def fit_to_screen(self):
        if not self.pil_image:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1:
            cw, ch = 1200, 700
        iw, ih = self.pil_image.size
        self.zoom_factor = min(cw / iw, ch / ih)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.show_image()

    def show_image(self):
        if not self.pil_image:
            return
        nw = int(self.pil_image.width * self.zoom_factor)
        nh = int(self.pil_image.height * self.zoom_factor)
        if nw < 1 or nh < 1:
            return
        resized = self.pil_image.resize((nw, nh), Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(resized, master=self.canvas)
        self.canvas.delete("all")
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                 anchor=tk.CENTER, image=self.tk_image)
        self.info_label.configure(text=f"Zoom: {int(self.zoom_factor * 100)}%")

    def zoom_in(self):
        self.zoom_factor *= 1.2
        self.show_image()

    def zoom_out(self):
        self.zoom_factor /= 1.2
        self.show_image()

    # ═══════════════════════════════════════════════════════ 액션
    def convert_to_pdf(self):
        indices = self.listbox.curselection()
        if not indices:
            messagebox.showwarning("Warning", "Please select images from the list first.")
            return
        selected_files = [self.display_paths[i] for i in indices if not self.display_paths[i].lower().endswith('.pdf')]
        if not selected_files:
            messagebox.showwarning("Warning", "No images selected (PDFs are excluded from merge).")
            return
        default_name = f"Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        save_path = filedialog.asksaveasfilename(defaultextension=".pdf",
                                                 filetypes=[("PDF files", "*.pdf")],
                                                 initialfile=default_name,
                                                 initialdir=os.path.dirname(selected_files[0]))
        if not save_path:
            return
        try:
            image_list = [Image.open(f).convert('RGB') for f in selected_files]
            if image_list:
                image_list[0].save(save_path, save_all=True, append_images=image_list[1:])
                messagebox.showinfo("Success", f"PDF created successfully:\n{os.path.basename(save_path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to create PDF:\n{e}")

    def show_loading_overlay(self, message="Processing..."):
        if not hasattr(self, 'loading_frame'):
            self.loading_frame = ctk.CTkFrame(self, corner_radius=14, fg_color="#2c2c2e",
                                              border_width=2, border_color=COL_ACCENT)
            self.loading_label = ctk.CTkLabel(
                self.loading_frame, text=message, font=ctk.CTkFont(size=16, weight="bold"),
                text_color="white")
            self.loading_label.pack(padx=50, pady=40)
        else:
            self.loading_label.configure(text=message)
        self.loading_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.update_idletasks()

    def hide_loading_overlay(self):
        if hasattr(self, 'loading_frame'):
            self.loading_frame.place_forget()

    def delete_selected(self):
        indices = self.listbox.curselection()
        if not indices:
            return
        if messagebox.askyesno("Confirm", f"Are you sure you want to delete {len(indices)} files?"):
            self.show_loading_overlay(f"🗑  Deleting {len(indices)} file(s)...")

            def delete_task():
                paths_to_delete = [self.display_paths[i] for i in sorted(indices, reverse=True)]
                for path in paths_to_delete:
                    try:
                        if path == self.current_pdf_path:
                            self.after(0, self._close_pdf)
                        os.remove(path)
                        self.full_image_paths.remove(path)
                    except Exception as e:
                        print(f"[ERROR] Error deleting {path}: {e}")

                def update_ui():
                    self.filter_images()
                    self.canvas.delete("all")
                    self.pil_image = None
                    self.hide_loading_overlay()

                self.after(0, update_ui)

            threading.Thread(target=delete_task, daemon=True).start()

    def destroy(self):
        try:
            self._close_pdf()
        except Exception:
            pass
        super().destroy()
