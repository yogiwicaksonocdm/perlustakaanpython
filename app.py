import os
import json
import uuid
import base64
import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash)
from werkzeug.security import generate_password_hash, check_password_hash

# Metode hash yang kompatibel di semua lingkungan (scrypt tidak selalu tersedia)
HASH_METHOD = "pbkdf2:sha256"
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
# Kunci sesi: WAJIB di-set via env SECRET_KEY saat produksi
app.secret_key = os.environ.get("SECRET_KEY", "kunci-dev-untuk-lokal-saja")
# Batas ukuran request (cover + form)
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 3 MB

# Inisialisasi Firebase Admin SDK
# Memeriksa apakah berjalan di Vercel (via Env Variable) atau Lokal (via file JSON)
if not firebase_admin._apps:
    if os.environ.get("FIREBASE_CREDENTIALS"):
        # Untuk Production (Vercel)
        cred_dict = json.loads(os.environ.get("FIREBASE_CREDENTIALS"))
        cred = credentials.Certificate(cred_dict)
    elif os.path.exists("serviceAccountKey.json"):
        # Untuk Pengujian Lokal
        cred = credentials.Certificate("serviceAccountKey.json")
    else:
        raise ValueError("Kunci Firebase SDK tidak ditemukan!")

    firebase_admin.initialize_app(cred)

db = firestore.client()

BOOKS = "books"
USERS = "users"
LOANS = "loans"


# --- HELPER AUTENTIKASI & PERAN ---

def get_current_user():
    """Ambil data user yang sedang login dari Firestore (tanpa hash password)."""
    uid = session.get("user_id")
    if not uid:
        return None
    doc = db.collection(USERS).document(uid).get()
    if not doc.exists:
        return None
    user = doc.to_dict()
    user["id"] = doc.id
    user.pop("password_hash", None)
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not get_current_user():
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        if user.get("role") != "admin":
            flash("Halaman ini hanya untuk admin.", "error")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_current_user():
    return {"current_user": get_current_user()}


def _now():
    return datetime.datetime.now()


def _fmt_date(dt):
    return dt.strftime("%d %b %Y")


# --- COVER BUKU ---

ALLOWED_COVER_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
MAX_COVER_SIZE = 500 * 1024  # 500 KB


def _cover_upload_to_data_uri():
    """Baca file cover dari form jadi data URI (base64) untuk disimpan di Firestore.

    Mengembalikan (data_uri, pesan_error). Filesystem Vercel bersifat read-only,
    jadi gambar disimpan langsung di dokumen buku sebagai base64.
    """
    file = request.files.get("cover")
    if not file or not file.filename:
        return None, None
    mime = (file.mimetype or "").lower()
    if mime not in ALLOWED_COVER_TYPES:
        return None, "Format cover harus PNG, JPG, WebP, atau GIF."
    data = file.read()
    if len(data) == 0:
        return None, "File cover kosong."
    if len(data) > MAX_COVER_SIZE:
        return None, "Ukuran cover maksimal 500 KB. Kompres dulu atau gunakan URL gambar."
    return "data:{};base64,".format(mime) + base64.b64encode(data).decode("ascii"), None


# --- ROUTES AUTENTIKASI ---

# Registrasi anggota (mandiri)
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Nama, email, dan password wajib diisi.", "error")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("Password minimal 6 karakter.", "error")
            return redirect(url_for("register"))

        # Email harus unik
        existing = [u for u in db.collection(USERS).stream()
                    if (u.to_dict().get("email") or "").lower() == email]
        if existing:
            flash("Email sudah terdaftar.", "error")
            return redirect(url_for("register"))

        # Pengguna pertama otomatis menjadi admin, sisanya anggota biasa
        role = "admin" if not any(True for _ in db.collection(USERS).limit(1).stream()) else "anggota"

        user_data = {
            "name": name,
            "email": email,
            "phone": phone,
            "address": address,
            "password_hash": generate_password_hash(password, method=HASH_METHOD),
            "role": role,
            "active": True,
            "joined_at": _now().strftime("%Y-%m-%d"),
        }
        _, ref = db.collection(USERS).add(user_data)
        session["user_id"] = ref.id
        flash("Registrasi berhasil. Selamat datang, {}!".format(name), "success")
        if role == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("member_dashboard"))

    return render_template("register.html")


# Login
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user_ref = None
        user_data = None
        for u in db.collection(USERS).stream():
            data = u.to_dict()
            if (data.get("email") or "").lower() == email:
                user_ref, user_data = u, data
                break

        if not user_data or not check_password_hash(
                user_data.get("password_hash", ""), password):
            flash("Email atau password salah.", "error")
            return redirect(url_for("login"))
        if not user_data.get("active", True):
            flash("Akun Anda diblokir. Hubungi admin.", "error")
            return redirect(url_for("login"))

        session["user_id"] = user_ref.id
        flash("Berhasil login. Halo, {}!".format(user_data.get("name")), "success")
        if user_data.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("member_dashboard"))

    return render_template("login.html")


# Logout
@app.route("/logout")
def logout():
    session.clear()
    flash("Anda telah keluar.", "success")
    return redirect(url_for("index"))


# --- DASHBOARD ---

# Dashboard Admin
@app.route("/admin")
@admin_required
def admin_dashboard():
    books = []
    for doc in db.collection(BOOKS).stream():
        data = doc.to_dict()
        data["id"] = doc.id
        data["display_status"] = "Tersedia" if int(data.get("stock", 0) or 0) > 0 else "Dipinjam"
        books.append(data)

    members = []
    for doc in db.collection(USERS).stream():
        data = doc.to_dict()
        data["id"] = doc.id
        data.pop("password_hash", None)
        members.append(data)
    members.sort(key=lambda m: m.get("name", "").lower())

    loans = []
    for doc in db.collection(LOANS).stream():
        data = doc.to_dict()
        data["id"] = doc.id
        loans.append(data)
    loans.sort(key=lambda l: l.get("borrow_date", ""), reverse=True)

    active_loans = [loan for loan in loans if loan.get("status", "dipinjam") == "dipinjam"]

    stats = {
        "total_books": len(books),
        "total_stock": sum(int(b.get("stock", 0) or 0) for b in books),
        "total_members": len(members),
        "active_loans": len(active_loans),
    }

    active_members = [m for m in members if m.get("active", True)]
    # Buku yang bisa dipinjam = punya stok tersedia > 0
    available_books = sorted(
        [b for b in books if int(b.get("stock", 0) or 0) > 0],
        key=lambda b: b.get("title", "").lower())

    # Jumlah peminjaman aktif per buku
    loan_counts = {}
    for loan in active_loans:
        bid = loan.get("book_id")
        if bid:
            loan_counts[bid] = loan_counts.get(bid, 0) + 1
    for b in books:
        b["loan_count"] = loan_counts.get(b["id"], 0)

    return render_template(
        "admin.html",
        stats=stats,
        books=books,
        members=members,
        loans=active_loans,
        active_members=active_members,
        available_books=available_books,
    )


# Dashboard Anggota
@app.route("/dashboard")
@login_required
def member_dashboard():
    user = get_current_user()

    my_loans = []
    for doc in db.collection(LOANS).stream():
        data = doc.to_dict()
        data["id"] = doc.id
        if data.get("user_id") == user["id"]:
            my_loans.append(data)
    my_loans.sort(key=lambda l: l.get("borrow_date", ""), reverse=True)
    active_loans = [loan for loan in my_loans if loan.get("status", "dipinjam") == "dipinjam"]

    return render_template(
        "dashboard.html",
        user=user,
        my_loans=my_loans,
        active_loans=active_loans,
    )


# --- PROFIL ANGGOTA ---

@app.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    user = get_current_user()

    if request.method == "POST":
        user_ref = db.collection(USERS).document(user["id"])
        update = {
            "name": request.form.get("name", "").strip() or user.get("name"),
            "phone": request.form.get("phone", "").strip(),
            "address": request.form.get("address", "").strip(),
        }
        new_password = request.form.get("password", "")
        if new_password:
            if len(new_password) < 6:
                flash("Password baru minimal 6 karakter.", "error")
                return redirect(url_for("profile"))
            update["password_hash"] = generate_password_hash(new_password, method=HASH_METHOD)
        user_ref.update(update)
        flash("Profil berhasil diperbarui.", "success")
        return redirect(url_for("profile"))

    my_loans = []
    for doc in db.collection(LOANS).stream():
        data = doc.to_dict()
        data["id"] = doc.id
        if data.get("user_id") == user["id"] and data.get("status", "dipinjam") == "dipinjam":
            my_loans.append(data)

    return render_template("profile.html", user=user, active_loans=my_loans)


# --- MANAJEMEN ANGGOTA (ADMIN) ---

# Tambah anggota dari dashboard admin
@app.route("/anggota/add", methods=["POST"])
@admin_required
def admin_add_member():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    password = request.form.get("password", "")

    if not name or not email or not password:
        flash("Nama, email, dan password wajib diisi.", "error")
        return redirect(url_for("admin_dashboard"))

    existing = [u for u in db.collection(USERS).stream()
                if (u.to_dict().get("email") or "").lower() == email]
    if existing:
        flash("Email sudah terdaftar.", "error")
        return redirect(url_for("admin_dashboard"))

    db.collection(USERS).add({
        "name": name,
        "email": email,
        "phone": phone,
        "address": address,
        "password_hash": generate_password_hash(password, method=HASH_METHOD),
        "role": "anggota",
        "active": True,
        "joined_at": _now().strftime("%Y-%m-%d"),
    })
    flash("Anggota baru '{}' berhasil ditambahkan.".format(name), "success")
    return redirect(url_for("admin_dashboard"))


# Blokir / buka blokir anggota
@app.route("/anggota/<member_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_member(member_id):
    if member_id == session.get("user_id"):
        flash("Anda tidak dapat memblokir akun sendiri.", "error")
        return redirect(url_for("admin_dashboard"))

    user_ref = db.collection(USERS).document(member_id)
    doc = user_ref.get()
    if not doc.exists:
        flash("Anggota tidak ditemukan.", "error")
        return redirect(url_for("admin_dashboard"))

    new_state = not doc.to_dict().get("active", True)
    user_ref.update({"active": new_state})
    flash("Status anggota diperbarui: {}".format("aktif" if new_state else "diblokir"), "success")
    return redirect(url_for("admin_dashboard"))


# Hapus anggota
@app.route("/anggota/<member_id>/delete", methods=["POST"])
@admin_required
def admin_delete_member(member_id):
    if member_id == session.get("user_id"):
        flash("Anda tidak dapat menghapus akun sendiri.", "error")
        return redirect(url_for("admin_dashboard"))

    db.collection(USERS).document(member_id).delete()
    flash("Anggota dihapus.", "success")
    return redirect(url_for("admin_dashboard"))


# --- PEMINJAMAN (ADMIN MENCATAT, ANGGOTA MELIHAT) ---

# Catat peminjaman baru
@app.route("/loans/add", methods=["POST"])
@admin_required
def admin_add_loan():
    member_id = request.form.get("member_id")
    book_id = request.form.get("book_id")

    member_doc = db.collection(USERS).document(member_id).get()
    book_doc = db.collection(BOOKS).document(book_id).get()
    if not member_doc.exists or not book_doc.exists:
        flash("Anggota atau buku tidak ditemukan.", "error")
        return redirect(url_for("admin_dashboard"))

    member = member_doc.to_dict()
    book = book_doc.to_dict()
    if not member.get("active", True):
        flash("Anggota sedang diblokir.", "error")
        return redirect(url_for("admin_dashboard"))

    # Stok tersedia harus > 0 boleh dipinjam (bisa menghadang stok >= 1)
    current_stock = int(book.get("stock", 0) or 0)
    if current_stock <= 0:
        flash("Stok buku sudah habis — semua salinan sedang dipinjam.", "error")
        return redirect(url_for("admin_dashboard"))

    now = _now()
    db.collection(LOANS).add({
        "user_id": member_id,
        "user_name": member.get("name", ""),
        "book_id": book_id,
        "book_title": book.get("title", ""),
        "borrow_date": _fmt_date(now),
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "status": "dipinjam",
        "return_date": "",
    })
    # Stok berkurang 1; status jadi Dipinjam hanya jika stok habis
    new_stock = current_stock - 1
    db.collection(BOOKS).document(book_id).update({
        "stock": new_stock,
        "status": "Dipinjam" if new_stock == 0 else "Tersedia",
    })
    flash("Peminjaman '{}' oleh {} dicatat. Sisa stok: {}".format(
        book.get("title"), member.get("name"), new_stock), "success")
    return redirect(url_for("admin_dashboard"))


# Tandai buku dikembalikan
@app.route("/loans/<loan_id>/return", methods=["POST"])
@admin_required
def admin_return_loan(loan_id):
    loan_ref = db.collection(LOANS).document(loan_id)
    doc = loan_ref.get()
    if not doc.exists:
        flash("Data peminjaman tidak ditemukan.", "error")
        return redirect(url_for("admin_dashboard"))

    loan = doc.to_dict()
    if loan.get("status") == "kembali":
        flash("Peminjaman ini sudah dikembalikan.", "error")
        return redirect(url_for("admin_dashboard"))

    loan_ref.update({
        "status": "kembali",
        "return_date": _fmt_date(_now()),
    })
    # Stok bertambah 1 saat buku dikembalikan
    if loan.get("book_id"):
        book_ref = db.collection(BOOKS).document(loan["book_id"])
        book_doc = book_ref.get()
        current_stock = int(book_doc.to_dict().get("stock", 0) or 0) if book_doc.exists else 0
        book_ref.update({"stock": current_stock + 1, "status": "Tersedia"})
    flash("Buku '{}' dikembalikan.".format(loan.get("book_title")), "success")
    return redirect(url_for("admin_dashboard"))


# --- ROUTES APLIKASI PERPUSTAKAAN (BUKU) ---

# 1. Tampilkan Semua Buku & Form Tambah Buku (dengan pencarian & filter)
@app.route("/")
def index():
    q = request.args.get("q", "").strip().lower()
    filter_category = request.args.get("category", "").strip()
    filter_status = request.args.get("status", "").strip()

    books = []
    categories = set()
    total_books_count = 0
    total_available = 0

    # Hitung jumlah peminjaman aktif per buku
    loan_counts = {}
    for doc in db.collection(LOANS).stream():
        loan_data = doc.to_dict()
        if loan_data.get("status", "dipinjam") == "dipinjam":
            bid = loan_data.get("book_id")
            if bid:
                loan_counts[bid] = loan_counts.get(bid, 0) + 1

    for doc in db.collection("books").stream():
        book_data = doc.to_dict()
        book_data["id"] = doc.id
        categories.add(book_data.get("category") or "Tanpa Kategori")
        # Statistik untuk hero halaman depan (di-hitung dari seluruh koleksi)
        total_books_count += 1
        if int(book_data.get("stock", 0) or 0) > 0:
            total_available += 1
        # Status tampil mengikuti sisa stok (stok 0 = semua salinan dipinjam)
        book_data["display_status"] = "Tersedia" if int(book_data.get("stock", 0) or 0) > 0 else "Dipinjam"
        book_data["loan_count"] = loan_counts.get(doc.id, 0)

        # Filter pencarian (judul, penulis, atau ISBN)
        if q:
            haystack = " ".join([
                str(book_data.get("title", "")),
                str(book_data.get("author", "")),
                str(book_data.get("isbn", "")),
            ]).lower()
            if q not in haystack:
                continue

        # Filter kategori
        if filter_category:
            book_category = book_data.get("category") or "Tanpa Kategori"
            if book_category != filter_category:
                continue

        # Filter status (Tersedia / Dipinjam) mengikuti sisa stok
        if filter_status and book_data["display_status"] != filter_status:
            continue

        books.append(book_data)

    categories = sorted(c for c in categories if c)
    hero_stats = {
        "total_books": total_books_count,
        "total_available": total_available,
        "total_categories": len(categories),
    }
    return render_template(
        "index.html",
        books=books,
        categories=categories,
        top_categories=categories[:6],
        hero_stats=hero_stats,
        current_q=request.args.get("q", ""),
        current_category=filter_category,
        current_status=filter_status,
    )

# 2. Tambah Buku Baru (admin saja)
@app.route("/add", methods=["POST"])
@admin_required
def add_book():
    title = request.form.get("title")
    author = request.form.get("author")
    year = request.form.get("year")
    isbn = request.form.get("isbn")
    category = request.form.get("category", "").strip()
    stock = request.form.get("stock")
    status = request.form.get("status", "Tersedia")
    cover_url = request.form.get("cover_url", "").strip()

    cover_data, cover_err = _cover_upload_to_data_uri()
    if cover_err:
        flash(cover_err, "error")
        return redirect(url_for("index"))

    if title and author:
        db.collection("books").add({
            "title": title,
            "author": author,
            "year": int(year) if year else 0,
            "isbn": isbn,
            "category": category,
            "stock": int(stock) if stock else 0,
            "status": status,
            "cover_data": cover_data or "",
            "cover_url": cover_url,
        })
        flash("Buku '{}' berhasil ditambahkan.".format(title), "success")
    return redirect(url_for("index"))

# 3. Edit Buku (admin saja)
@app.route("/edit/<book_id>", methods=["GET", "POST"])
@admin_required
def edit_book(book_id):
    book_ref = db.collection("books").document(book_id)
    if request.method == "POST":
        update_data = {
            "title": request.form.get("title"),
            "author": request.form.get("author"),
            "year": int(request.form.get("year")),
            "isbn": request.form.get("isbn"),
            "category": request.form.get("category", "").strip(),
            "stock": int(request.form.get("stock") or 0),
            "status": request.form.get("status")
        }
        if request.form.get("remove_cover"):
            update_data["cover_data"] = ""
            update_data["cover_url"] = ""
        else:
            cover_data, cover_err = _cover_upload_to_data_uri()
            if cover_err:
                flash(cover_err, "error")
                return redirect(url_for("edit_book", book_id=book_id))
            if cover_data:
                update_data["cover_data"] = cover_data
            new_cover_url = request.form.get("cover_url", "").strip()
            if new_cover_url:
                update_data["cover_url"] = new_cover_url
        book_ref.update(update_data)
        flash("Data buku diperbarui.", "success")
        return redirect(url_for("index"))
    doc = book_ref.get()
    if doc.exists:
        book = doc.to_dict()
        book["id"] = doc.id
        return render_template("edit.html", book=book)
    flash("Buku tidak ditemukan.", "error")
    return redirect(url_for("index"))

# 4. Hapus Buku (admin saja)
@app.route("/delete/<book_id>")
@admin_required
def delete_book(book_id):
    db.collection("books").document(book_id).delete()
    flash("Buku dihapus.", "success")
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)
