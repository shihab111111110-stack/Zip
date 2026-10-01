import os, sqlite3, uuid, shutil, subprocess, secrets
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import bcrypt

BASE = Path(__file__).parent.resolve()
DB = BASE / "hosting.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key")

app = FastAPI(title="Telegram Hosting V1")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory=str(BASE / "templates"))

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con=db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      is_admin INTEGER DEFAULT 0,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS plans(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      price REAL NOT NULL,
      ram_mb INTEGER NOT NULL,
      storage_mb INTEGER NOT NULL,
      bot_limit INTEGER NOT NULL,
      active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS subscriptions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      plan_id INTEGER NOT NULL,
      status TEXT DEFAULT 'pending',
      expires_at TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bots(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      folder TEXT NOT NULL,
      status TEXT DEFAULT 'stopped',
      container TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payments(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      method TEXT NOT NULL,
      amount REAL NOT NULL,
      trx_id TEXT NOT NULL,
      status TEXT DEFAULT 'pending',
      created_at TEXT NOT NULL
    );
    """)
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute("INSERT INTO users(username,password,is_admin,created_at) VALUES(?,?,1,?)",
                    (ADMIN_USERNAME,bcrypt.hash(ADMIN_PASSWORD),datetime.utcnow().isoformat()))
    if con.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
        con.executemany("INSERT INTO plans(name,price,ram_mb,storage_mb,bot_limit) VALUES(?,?,?,?,?)", [
            ("Starter",49,256,200,1), ("Basic",99,512,500,2), ("Pro",199,1024,1500,5)
        ])
    con.commit(); con.close()

init_db()

def user(request):
    uid=request.session.get("uid")
    if not uid: return None
    con=db(); u=con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(); con.close()
    return u

def redirect(url): return RedirectResponse(url, status_code=303)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request":request, "user":user(request)})

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request":request})

@app.post("/register")
def register(username: str=Form(...), password: str=Form(...)):
    username=username.strip()
    if len(username)<3 or len(password)<6:
        return HTMLResponse("Username 3+ chars and password 6+ chars required.",400)
    con=db()
    try:
        con.execute("INSERT INTO users(username,password,created_at) VALUES(?,?,?)",
                    (username,bcrypt.hash(password),datetime.utcnow().isoformat()))
        con.commit()
    except sqlite3.IntegrityError:
        con.close(); return HTMLResponse("Username already exists.",400)
    con.close(); return redirect("/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request":request})

@app.post("/login")
def login(request: Request, username: str=Form(...), password: str=Form(...)):
    con=db(); u=con.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone(); con.close()
    if not u or not bcrypt.verify(password,u["password"]):
        return HTMLResponse("Invalid username or password.",400)
    request.session["uid"]=u["id"]
    return redirect("/admin" if u["is_admin"] else "/dashboard")

@app.get("/logout")
def logout(request: Request):
    request.session.clear(); return redirect("/")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    u=user(request)
    if not u: return redirect("/login")
    con=db()
    bots=con.execute("SELECT * FROM bots WHERE user_id=? ORDER BY id DESC",(u["id"],)).fetchall()
    plans=con.execute("SELECT * FROM plans WHERE active=1").fetchall()
    payments=con.execute("SELECT * FROM payments WHERE user_id=? ORDER BY id DESC",(u["id"],)).fetchall()
    con.close()
    return templates.TemplateResponse("dashboard.html",{"request":request,"user":u,"bots":bots,"plans":plans,"payments":payments})

@app.post("/payment")
def payment(request: Request, method:str=Form(...), amount:float=Form(...), trx_id:str=Form(...)):
    u=user(request)
    if not u: return redirect("/login")
    if method not in ("bKash","Nagad") or amount<=0 or not trx_id.strip():
        return HTMLResponse("Invalid payment data.",400)
    con=db(); con.execute("INSERT INTO payments(user_id,method,amount,trx_id,created_at) VALUES(?,?,?,?,?)",
                           (u["id"],method,amount,trx_id.strip(),datetime.utcnow().isoformat()))
    con.commit(); con.close(); return redirect("/dashboard")

@app.post("/bot/create")
def create_bot(request: Request, name:str=Form(...), code:UploadFile=File(...)):
    u=user(request)
    if not u: return redirect("/login")
    safe="".join(c for c in name if c.isalnum() or c in "-_")[:32] or "bot"
    con=db()
    count=con.execute("SELECT COUNT(*) FROM bots WHERE user_id=?",(u["id"],)).fetchone()[0]
    sub=con.execute("""SELECT p.* FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                       WHERE s.user_id=? AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?)
                       ORDER BY s.id DESC LIMIT 1""",(u["id"],datetime.utcnow().isoformat())).fetchone()
    limit=sub["bot_limit"] if sub else 0
    if count>=limit:
        con.close(); return HTMLResponse("No active plan or bot limit reached.",400)
    botdir=UPLOADS/f"{u['id']}_{uuid.uuid4().hex[:10]}"
    botdir.mkdir(parents=True)
    if not code.filename.endswith(".py"):
        shutil.rmtree(botdir); con.close(); return HTMLResponse("Upload a .py bot file.",400)
    with open(botdir/"bot.py","wb") as f: shutil.copyfileobj(code.file,f)
    (botdir/"requirements.txt").write_text("",encoding="utf-8")
    con.execute("INSERT INTO bots(user_id,name,folder,created_at) VALUES(?,?,?,?)",
                (u["id"],safe,str(botdir),datetime.utcnow().isoformat()))
    con.commit(); con.close(); return redirect("/dashboard")

def docker_cmd(bot):
    cname=f"tg-host-{bot['id']}"
    folder=Path(bot["folder"])
    if bot["status"]=="running":
        subprocess.run(["docker","rm","-f",cname],capture_output=True,text=True)
    # The container has no host-network access and uses a bounded memory/CPU budget.
    cmd=["docker","run","-d","--name",cname,"--restart","unless-stopped",
         "--memory","512m","--cpus","0.50","--pids-limit","128",
         "--network","bridge","-v",f"{folder}:/app:ro","python:3.12-slim",
         "sh","-c","pip install --no-cache-dir -r /app/requirements.txt >/tmp/pip.log 2>&1; exec python /app/bot.py"]
    r=subprocess.run(cmd,capture_output=True,text=True)
    return cname,r

@app.post("/bot/{bot_id}/{action}")
def bot_action(request: Request, bot_id:int, action:str):
    u=user(request)
    if not u: return redirect("/login")
    con=db(); bot=con.execute("SELECT * FROM bots WHERE id=? AND user_id=?",(bot_id,u["id"])).fetchone()
    if not bot: con.close(); return HTMLResponse("Bot not found.",404)
    if action=="start":
        cname,r=docker_cmd(bot)
        if r.returncode==0: con.execute("UPDATE bots SET status='running',container=? WHERE id=?",(cname,bot_id))
    elif action=="stop":
        subprocess.run(["docker","rm","-f",bot["container"] or f"tg-host-{bot_id}"],capture_output=True)
        con.execute("UPDATE bots SET status='stopped' WHERE id=?",(bot_id,))
    elif action=="restart":
        subprocess.run(["docker","rm","-f",bot["container"] or f"tg-host-{bot_id}"],capture_output=True)
        cname,r=docker_cmd(bot)
        if r.returncode==0: con.execute("UPDATE bots SET status='running',container=? WHERE id=?",(cname,bot_id))
    elif action=="delete":
        subprocess.run(["docker","rm","-f",bot["container"] or f"tg-host-{bot_id}"],capture_output=True)
        shutil.rmtree(bot["folder"],ignore_errors=True)
        con.execute("DELETE FROM bots WHERE id=?",(bot_id,))
    con.commit(); con.close(); return redirect("/dashboard")

@app.get("/bot/{bot_id}/logs", response_class=HTMLResponse)
def logs(request:Request, bot_id:int):
    u=user(request)
    if not u: return redirect("/login")
    con=db(); bot=con.execute("SELECT * FROM bots WHERE id=? AND user_id=?",(bot_id,u["id"])).fetchone(); con.close()
    if not bot: return HTMLResponse("Not found",404)
    r=subprocess.run(["docker","logs","--tail","200",bot["container"] or f"tg-host-{bot_id}"],capture_output=True,text=True)
    return HTMLResponse("<pre>"+(r.stdout+r.stderr).replace("&","&amp;").replace("<","&lt;")+"</pre><p><a href='/dashboard'>Back</a></p>")

@app.get("/admin", response_class=HTMLResponse)
def admin(request:Request):
    u=user(request)
    if not u or not u["is_admin"]: return redirect("/login")
    con=db()
    users=con.execute("SELECT id,username,is_admin,created_at FROM users ORDER BY id DESC").fetchall()
    plans=con.execute("SELECT * FROM plans ORDER BY id").fetchall()
    payments=con.execute("""SELECT p.*,u.username FROM payments p JOIN users u ON u.id=p.user_id
                            ORDER BY p.id DESC""").fetchall()
    con.close()
    return templates.TemplateResponse("admin.html",{"request":request,"user":u,"users":users,"plans":plans,"payments":payments})

@app.post("/admin/plan")
def add_plan(request:Request,name:str=Form(...),price:float=Form(...),ram_mb:int=Form(...),storage_mb:int=Form(...),bot_limit:int=Form(...)):
    u=user(request)
    if not u or not u["is_admin"]: return redirect("/login")
    con=db(); con.execute("INSERT INTO plans(name,price,ram_mb,storage_mb,bot_limit) VALUES(?,?,?,?,?)",
                           (name,price,ram_mb,storage_mb,bot_limit)); con.commit(); con.close()
    return redirect("/admin")

@app.post("/admin/payment/{pid}/approve")
def approve_payment(request:Request,pid:int):
    u=user(request)
    if not u or not u["is_admin"]: return redirect("/login")
    con=db(); p=con.execute("SELECT * FROM payments WHERE id=?",(pid,)).fetchone()
    if p:
        plan=con.execute("SELECT * FROM plans WHERE active=1 AND price<=? ORDER BY price DESC LIMIT 1",(p["amount"],)).fetchone()
        if plan:
            con.execute("UPDATE payments SET status='approved' WHERE id=?",(pid,))
            con.execute("UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active'",(p["user_id"],))
            con.execute("INSERT INTO subscriptions(user_id,plan_id,status,created_at) VALUES(?,?,?,?)",
                         (p["user_id"],plan["id"],"active",datetime.utcnow().isoformat()))
    con.commit(); con.close(); return redirect("/admin")
