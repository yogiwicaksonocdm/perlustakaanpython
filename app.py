import os
import json
from flask import Flask, render_template, request, redirect, url_for
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

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

# --- ROUTES APLIKASI PERPUSTAKAAN ---

# 1. Tampilkan Semua Buku & Form Tambah Buku
@app.route("/")
def index():
    books_ref = db.collection("books")
    docs = books_ref.stream()
    books = []
    for doc in docs:
        book_data = doc.to_dict()
        book_data["id"] = doc.id
        books.append(book_data)
    return render_template("index.html", books=books)

# 2. Tambah Buku Baru
@app.route("/add", methods=["POST"])
def add_book():
    title = request.form.get("title")
    author = request.form.get("author")
    year = request.form.get("year")
    isbn = request.form.get("isbn")
    status = request.form.get("status", "Tersedia")

    if title and author:
        db.collection("books").add({
            "title": title,
            "author": author,
            "year": int(year) if year else 0,
            "isbn": isbn,
            "status": status
        })
    return redirect(url_for("index"))

# 3. Edit Buku
@app.route("/edit/<book_id>", methods=["GET", "POST"])
def edit_book(book_id):
    book_ref = db.collection("books").document(book_id)
    if request.method == "POST":
        book_ref.update({
            "title": request.form.get("title"),
            "author": request.form.get("author"),
            "year": int(request.form.get("year")),
            "isbn": request.form.get("isbn"),
            "status": request.form.get("status")
        })
        return redirect(url_for("index"))
    
    doc = book_ref.get()
    if doc.exists:
        book = doc.to_dict()
        book["id"] = doc.id
        return render_template("edit.html", book=book)
    return redirect(url_for("index"))

# 4. Hapus Buku
@app.route("/delete/<book_id>")
def delete_book(book_id):
    db.collection("books").document(book_id).delete()
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)