"""
app.py — Smart Notes & Study Guide Builder
Main Flask application entry point.

Run with:
    venv\\Scripts\\python app.py        (Windows)
    python app.py                      (macOS/Linux)
"""
import os
import json
import logging
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, session, abort,
)
from werkzeug.exceptions import RequestEntityTooLarge

from config import Config
from database.db import init_db, get_db, log_activity
from services.document_service import (
    allowed_file, validate_upload, save_upload, extract_text,
)
from services.ai_service import (
    ai_available, generate_summary, generate_study_guide,
    generate_flashcards, generate_quiz, answer_question,
)
from services.study_guide_service import (
    save_note, get_saved_note, list_saved_notes, delete_note,
)

# ─────────────────────────────────────────────────────────────────────────────
#  App setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)
app.config["UPLOAD_FOLDER"] = Config.UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_CONTENT_LENGTH

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

with app.app_context():
    init_db()


# ─────────────────────────────────────────────────────────────────────────────
#  Context processors & error handlers
# ─────────────────────────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return {"ai_available": ai_available()}


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    flash(f"File too large. Maximum allowed size is {Config.MAX_CONTENT_MB} MB.", "error")
    return redirect(url_for("upload"))


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(_):
    return render_template("500.html"), 500


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    with get_db() as conn:
        notebook_count = conn.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0]
        doc_count      = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        recent_docs    = conn.execute(
            "SELECT d.*, n.name AS notebook_name FROM documents d "
            "LEFT JOIN notebooks n ON n.id = d.notebook_id "
            "ORDER BY d.created_at DESC LIMIT 5"
        ).fetchall()
        activity       = conn.execute(
            "SELECT * FROM study_activity ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
    return render_template(
        "dashboard.html",
        notebook_count=notebook_count,
        doc_count=doc_count,
        recent_docs=recent_docs,
        activity=activity,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Notebooks
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/notebooks")
def notebooks():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT n.*, COUNT(d.id) AS doc_count
               FROM notebooks n
               LEFT JOIN documents d ON d.notebook_id = n.id
               GROUP BY n.id ORDER BY n.updated_at DESC"""
        ).fetchall()
    return render_template("notebooks.html", notebooks=rows)


@app.route("/notebooks/create", methods=["POST"])
def create_notebook():
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    if not name:
        flash("Notebook name is required.", "error")
        return redirect(url_for("notebooks"))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notebooks (name, description) VALUES (?, ?)", (name, desc)
        )
    flash(f'Notebook "{name}" created.', "success")
    log_activity("Created notebook", name)
    return redirect(url_for("notebooks"))


@app.route("/notebooks/<int:nb_id>")
def notebook_detail(nb_id):
    with get_db() as conn:
        nb = conn.execute("SELECT * FROM notebooks WHERE id = ?", (nb_id,)).fetchone()
        if not nb:
            abort(404)
        docs = conn.execute(
            "SELECT * FROM documents WHERE notebook_id = ? ORDER BY created_at DESC", (nb_id,)
        ).fetchall()
        notes = list_saved_notes()
    return render_template("notebook_detail.html", notebook=nb, docs=docs, notes=notes)


@app.route("/notebooks/<int:nb_id>/rename", methods=["POST"])
def rename_notebook(nb_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name cannot be empty.", "error")
        return redirect(url_for("notebook_detail", nb_id=nb_id))
    with get_db() as conn:
        conn.execute(
            "UPDATE notebooks SET name=?, updated_at=datetime('now') WHERE id=?", (name, nb_id)
        )
    flash("Notebook renamed.", "success")
    return redirect(url_for("notebook_detail", nb_id=nb_id))


@app.route("/notebooks/<int:nb_id>/delete", methods=["POST"])
def delete_notebook(nb_id):
    with get_db() as conn:
        conn.execute("DELETE FROM notebooks WHERE id = ?", (nb_id,))
    flash("Notebook deleted.", "success")
    return redirect(url_for("notebooks"))


# ─────────────────────────────────────────────────────────────────────────────
#  Document upload
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["GET", "POST"])
def upload():
    with get_db() as conn:
        nbs = conn.execute("SELECT id, name FROM notebooks ORDER BY name").fetchall()

    if request.method == "POST":
        file = request.files.get("file")
        ok, err = validate_upload(file)
        if not ok:
            flash(err, "error")
            return redirect(url_for("upload"))

        notebook_id = request.form.get("notebook_id") or None
        if notebook_id:
            notebook_id = int(notebook_id)

        try:
            safe_name, original_name = save_upload(file)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("upload"))

        # Extract text
        text, pages, warning = extract_text(safe_name)
        status = "processed" if text else "error"
        file_size = os.path.getsize(os.path.join(Config.UPLOAD_FOLDER, safe_name))
        file_type = safe_name.rsplit(".", 1)[-1].lower()

        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO documents
                   (notebook_id, filename, original_name, file_type, file_size,
                    extracted_text, page_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (notebook_id, safe_name, original_name, file_type,
                 file_size, text, pages, status),
            )
            doc_id = cur.lastrowid

        log_activity("Uploaded document", original_name)

        if warning:
            flash(f"⚠ {warning}", "warning")
        if text:
            flash(f'"{original_name}" uploaded and processed successfully ({pages} page(s)).', "success")
        else:
            flash(f'"{original_name}" uploaded but no text was extracted.', "warning")

        return redirect(url_for("document_detail", doc_id=doc_id))

    with get_db() as conn:
        recent = conn.execute(
            "SELECT * FROM documents ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    return render_template("upload.html", notebooks=nbs, recent_docs=recent)


@app.route("/documents/<int:doc_id>")
def document_detail(doc_id):
    with get_db() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            abort(404)
        saved = list_saved_notes(document_id=doc_id)
        nbs   = conn.execute("SELECT id, name FROM notebooks ORDER BY name").fetchall()
    return render_template("document_detail.html", doc=doc, saved_notes=saved, notebooks=nbs)


@app.route("/documents/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    with get_db() as conn:
        doc = conn.execute("SELECT filename FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc:
            filepath = os.path.join(Config.UPLOAD_FOLDER, doc["filename"])
            if os.path.exists(filepath):
                os.remove(filepath)
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    flash("Document deleted.", "success")
    return redirect(url_for("upload"))


# ─────────────────────────────────────────────────────────────────────────────
#  AI Notes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/notes")
def notes_list():
    saved = list_saved_notes(note_type="summary")
    with get_db() as conn:
        docs = conn.execute(
            "SELECT id, original_name FROM documents WHERE status='processed' ORDER BY created_at DESC"
        ).fetchall()
    return render_template("notes.html", saved_notes=saved, docs=docs)


@app.route("/notes/generate", methods=["POST"])
def generate_notes():
    doc_id = request.form.get("document_id")
    if not doc_id:
        flash("Please select a document.", "error")
        return redirect(url_for("notes_list"))

    with get_db() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (int(doc_id),)
        ).fetchone()

    if not doc or not doc["extracted_text"]:
        flash("Document has no extractable text.", "error")
        return redirect(url_for("notes_list"))

    result = generate_summary(doc["extracted_text"])
    note_id = save_note(int(doc_id), doc["notebook_id"], "summary", result["content"])
    log_activity("Generated summary", doc["original_name"])

    if result["warning"]:
        flash(result["warning"], "warning")

    return redirect(url_for("view_note", note_id=note_id))


@app.route("/notes/<int:note_id>")
def view_note(note_id):
    note = get_saved_note(note_id)
    if not note:
        abort(404)
    return render_template("view_note.html", note=note, note_type_label="Smart Notes")


# ─────────────────────────────────────────────────────────────────────────────
#  Study Guides
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/study-guide")
def study_guide_list():
    saved = list_saved_notes(note_type="study_guide")
    with get_db() as conn:
        docs = conn.execute(
            "SELECT id, original_name FROM documents WHERE status='processed' ORDER BY created_at DESC"
        ).fetchall()
    return render_template("study_guide.html", saved_notes=saved, docs=docs)


@app.route("/study-guide/generate", methods=["POST"])
def generate_study_guide_route():
    doc_id = request.form.get("document_id")
    mode   = request.form.get("mode", "concise")
    if not doc_id:
        flash("Please select a document.", "error")
        return redirect(url_for("study_guide_list"))

    with get_db() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()

    if not doc or not doc["extracted_text"]:
        flash("Document has no extractable text.", "error")
        return redirect(url_for("study_guide_list"))

    result = generate_study_guide(doc["extracted_text"], mode=mode)
    note_id = save_note(int(doc_id), doc["notebook_id"], "study_guide", result["content"])
    log_activity("Generated study guide", doc["original_name"])

    if result["warning"]:
        flash(result["warning"], "warning")

    return redirect(url_for("view_note", note_id=note_id))


# ─────────────────────────────────────────────────────────────────────────────
#  Flashcards
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/flashcards")
def flashcards_list():
    saved = list_saved_notes(note_type="flashcards")
    with get_db() as conn:
        docs = conn.execute(
            "SELECT id, original_name FROM documents WHERE status='processed' ORDER BY created_at DESC"
        ).fetchall()
    return render_template("flashcards.html", saved_notes=saved, docs=docs)


@app.route("/flashcards/generate", methods=["POST"])
def generate_flashcards_route():
    doc_id = request.form.get("document_id")
    if not doc_id:
        flash("Please select a document.", "error")
        return redirect(url_for("flashcards_list"))

    with get_db() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()

    if not doc or not doc["extracted_text"]:
        flash("Document has no extractable text.", "error")
        return redirect(url_for("flashcards_list"))

    result = generate_flashcards(doc["extracted_text"])
    note_id = save_note(int(doc_id), doc["notebook_id"], "flashcards", result["cards"])
    log_activity("Generated flashcards", doc["original_name"])

    if result["warning"]:
        flash(result["warning"], "warning")

    return redirect(url_for("view_flashcards", note_id=note_id))


@app.route("/flashcards/<int:note_id>")
def view_flashcards(note_id):
    note = get_saved_note(note_id)
    if not note:
        abort(404)
    cards = note["content"]
    if isinstance(cards, str):
        try:
            cards = json.loads(cards)
        except Exception:
            cards = []
    return render_template("flashcard_viewer.html", note=note, cards=cards)


# ─────────────────────────────────────────────────────────────────────────────
#  Practice Quiz
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/quiz")
def quiz_list():
    saved = list_saved_notes(note_type="quiz")
    with get_db() as conn:
        docs = conn.execute(
            "SELECT id, original_name FROM documents WHERE status='processed' ORDER BY created_at DESC"
        ).fetchall()
    return render_template("quiz.html", saved_notes=saved, docs=docs)


@app.route("/quiz/generate", methods=["POST"])
def generate_quiz_route():
    doc_id = request.form.get("document_id")
    if not doc_id:
        flash("Please select a document.", "error")
        return redirect(url_for("quiz_list"))

    with get_db() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()

    if not doc or not doc["extracted_text"]:
        flash("Document has no extractable text.", "error")
        return redirect(url_for("quiz_list"))

    result = generate_quiz(doc["extracted_text"])
    note_id = save_note(int(doc_id), doc["notebook_id"], "quiz", result["questions"])
    log_activity("Generated quiz", doc["original_name"])

    if result["warning"]:
        flash(result["warning"], "warning")

    return redirect(url_for("take_quiz", note_id=note_id))


@app.route("/quiz/<int:note_id>")
def take_quiz(note_id):
    note = get_saved_note(note_id)
    if not note:
        abort(404)
    questions = note["content"]
    if isinstance(questions, str):
        try:
            questions = json.loads(questions)
        except Exception:
            questions = []
    return render_template("quiz_viewer.html", note=note, questions=questions)


# ─────────────────────────────────────────────────────────────────────────────
#  AI Chat (source-based Q&A)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/chat")
def chat():
    with get_db() as conn:
        docs = conn.execute(
            "SELECT id, original_name FROM documents WHERE status='processed' ORDER BY created_at DESC"
        ).fetchall()
    return render_template("chat.html", docs=docs)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    question   = (data.get("question") or "").strip()
    doc_ids    = data.get("doc_ids") or []

    if not question:
        return jsonify({"error": "Question is required."}), 400

    if not doc_ids:
        return jsonify({"error": "Select at least one document."}), 400

    with get_db() as conn:
        placeholders = ",".join("?" * len(doc_ids))
        docs = conn.execute(
            f"SELECT original_name, extracted_text, page_count FROM documents WHERE id IN ({placeholders})",
            doc_ids,
        ).fetchall()

    result = answer_question(question, [dict(d) for d in docs])
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
#  Saved notes — delete
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/notes/<int:note_id>/delete", methods=["POST"])
def delete_saved_note(note_id):
    delete_note(note_id)
    flash("Note deleted.", "success")
    return redirect(url_for("notes_list"))


# ─────────────────────────────────────────────────────────────────────────────
#  Settings
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        ai_provider=Config.AI_PROVIDER,
        ai_available=ai_available(),
        max_upload_mb=Config.MAX_CONTENT_MB,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=Config.DEBUG, host="127.0.0.1", port=5000)
