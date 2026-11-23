"""Compact server-rendered role UI. Local OTP delivery is console-only in development."""
from __future__ import annotations

import hashlib
import hmac
import asyncio
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage
import json
import html
import logging
import math
import os
import re
import secrets
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import httpx
from app.fulfillment import offer_route_to_next_driver, quote_basket, queue_order_route, release_order_offers
from app.geocoding import geocode_address
from app.schemas import PlanningJobCreate
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger("fresh_produce_ui")
router = APIRouter()


def otp_digest(code: str) -> str:
    pepper = os.getenv("SESSION_SECRET") or os.getenv("API_KEY") or "local-development-only"
    return hmac.new(pepper.encode(), code.encode(), hashlib.sha256).hexdigest()


def is_image(content_type: str, data: bytes) -> bool:
    return (
        content_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
        or content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")
        or content_type == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    )


def valid_quantity(value: Decimal, allow_zero: bool = False) -> bool:
    minimum = Decimal("0") if allow_zero else Decimal("0.001")
    return value.is_finite() and value >= minimum and value.as_tuple().exponent >= -3 and value.adjusted() < 9


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def page(title: str, body: str, user: dict | None = None, notice: str = "", cart_count: int = 0) -> HTMLResponse:
    if user:
        identity = f'<span class="identity">{esc(user["full_name"])} <small>{esc(user["role"])}</small></span><a class="nav-button" href="/ui/dashboard">My dashboard</a><form method="post" action="/ui/logout"><input type="hidden" name="csrf" value="{esc(user["csrf"])}"><button class="nav-button light">Sign out</button></form>'
    else:
        identity = '<a class="nav-button light" href="/ui/login">Sign in</a>'
    nav = f'''<header class="site-header"><a class="brand" href="/ui"><span class="brand-mark">♧</span><span><b>FarmFresh</b><small>Good food. Good life.</small></span></a><nav><a href="/ui">Home</a><a href="/ui#categories">Categories</a><a href="/ui#shop">Shop</a><a href="/ui#how">How it works</a></nav><div class="header-actions"><a class="cart-link" href="/ui/cart">Bag <b>{cart_count}</b></a>{identity}</div></header>'''
    message = f'<div class="notice">{esc(notice)}</div>' if notice else ""
    return HTMLResponse(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#164b2e"><title>{esc(title)} · FarmFresh</title><style>
*{{box-sizing:border-box}}:root{{--green:#154c2e;--green2:#2e6c43;--lime:#d8e7ba;--cream:#f8f8f2;--ink:#21342a;--muted:#718073;--line:#e5e9df}}body{{margin:0;background:var(--cream);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}}a{{color:var(--green);text-decoration:none}}button{{font:inherit;cursor:pointer}}.site-header{{height:76px;background:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1220px)/2));border-bottom:1px solid #edf0e9;gap:24px;position:sticky;top:0;z-index:5}}.brand{{display:flex;align-items:center;gap:9px;color:var(--green)}}.brand-mark{{display:grid;place-items:center;width:38px;height:38px;background:#eaf2e2;border-radius:50%;font-size:25px}}.brand b{{display:block;font:700 20px Georgia,serif;letter-spacing:-.3px}}.brand small{{display:block;font-size:10px;color:#67796a;line-height:1}}nav{{display:flex;gap:31px}}nav a{{font-size:13px;color:#39473d}}nav a:hover{{color:var(--green2)}}.header-actions{{display:flex;align-items:center;gap:12px}}.identity{{font-size:12px;text-align:right}}.identity small{{display:block;color:var(--muted)}}.cart-link{{font-size:13px;font-weight:650;padding:9px 13px;border:1px solid var(--line);border-radius:25px}}.cart-link b{{display:inline-grid;place-items:center;background:var(--green);color:#fff;border-radius:50%;width:21px;height:21px;margin-left:4px}}.nav-button,.button,.primary{{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;background:var(--green);color:#fff;border-radius:9px;padding:11px 18px;font-weight:650;font-size:13px}}.nav-button.light{{background:#fff;color:var(--green);border:1px solid #d8e1d5}}header form{{margin:0}}main{{max-width:1220px;margin:26px auto 70px;padding:0 24px}}h1,h2,h3{{color:var(--green)}}h1{{font:700 clamp(34px,4vw,56px)/1.04 Georgia,serif;letter-spacing:-1.5px}}h2{{font:700 27px/1.2 Georgia,serif;margin:12px 0}}h3{{font-size:18px}}p{{margin:8px 0}}.muted,small{{color:var(--muted)}}section,.card{{background:#fff;border:1px solid var(--line);border-radius:15px;padding:21px;margin:14px 0;box-shadow:0 6px 24px #1a4c2608}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}}label{{display:block;font-size:13px;font-weight:650;margin:12px 0 5px}}input,select,textarea{{width:100%;padding:12px;border:1px solid #d7dfd4;border-radius:9px;background:#fff;font:inherit}}input[type=checkbox]{{width:auto}}button{{margin-top:10px}}button:not(.nav-button),.primary{{background:var(--green);color:white;border:0;border-radius:9px;padding:12px 17px;font-weight:650}}button:hover,.primary:hover{{background:#27633d}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{font-size:12px;color:#5c6d5f}}.notice{{max-width:1170px;margin:18px auto 0;padding:12px 18px;border-radius:10px;background:#e5f2e4;color:var(--green)}}.error{{background:#fff0ed;color:#9b3326;padding:12px;border-radius:9px}}.hero{{display:grid;grid-template-columns:1fr 1fr;min-height:420px;background:#f2f3e9;border-radius:20px;overflow:hidden;margin-top:24px}}.hero-copy{{padding:52px 46px;align-self:center}}.eyebrow{{text-transform:uppercase;letter-spacing:2px;font-size:11px;color:#688047;font-weight:750}}.hero-copy h1{{margin:15px 0;color:#173f27}}.hero-copy p{{max-width:420px;color:#667367;font-size:16px;margin:0 0 22px}}.hero-actions{{display:flex;gap:11px;flex-wrap:wrap}}.secondary{{display:inline-flex;align-items:center;border:1px solid #cad4c6;border-radius:9px;padding:11px 18px;color:var(--green);background:#fff;font-size:13px;font-weight:650}}.hero-photo{{min-height:420px;background:#e5e9da;position:relative}}.hero-photo img{{width:100%;height:100%;position:absolute;inset:0;object-fit:cover}}.hero-note{{position:absolute;bottom:20px;left:20px;background:#fff;padding:12px 16px;border-radius:12px;box-shadow:0 8px 20px #0002;font-weight:650;color:var(--green)}}.trust-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:20px 5px;color:#526650;font-size:13px}}.trust-row span{{display:flex;align-items:center;gap:9px}}.section-head{{text-align:center;margin:52px auto 23px}}.section-head .eyebrow{{font-size:10px}}.category-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:14px}}.category-card{{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;color:var(--ink);transition:.18s transform}}.category-card:hover,.product-card:hover{{transform:translateY(-3px)}}.category-card img{{width:100%;height:135px;object-fit:cover}}.category-card div{{padding:12px 14px}}.category-card b{{display:block;font-size:13px}}.category-card small{{font-size:11px}}.product-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px}}.product-card{{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;transition:.18s transform}}.product-card img{{width:100%;height:160px;object-fit:cover;background:#eff3e8}}.product-info{{padding:15px}}.product-info h3{{margin:0;font:700 18px Georgia,serif}}.product-info p{{font-size:12px;color:var(--muted)}}.stock{{display:inline-block;color:#39744a;background:#eef5e8;padding:4px 8px;border-radius:20px;font-size:10px;font-weight:700;margin:5px 0}}.add-row{{display:flex;gap:8px;align-items:center}}.add-row input{{width:74px;padding:9px}}.add-row button{{margin:0;flex:1;padding:10px}}.how-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.how-card{{padding:19px;border-radius:12px;background:#f7f8f2}}.how-card b{{color:var(--green)}}.site-footer{{background:var(--green);color:#eff5e9;padding:26px max(24px,calc((100vw - 1220px)/2));display:flex;justify-content:space-between;font-size:12px}}.form-narrow{{max-width:580px;margin:50px auto}}.form-narrow>section{{padding:30px}}.address-options{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}.address-options button{{margin:0}}.location-status{{font-size:12px;color:var(--green)}}
.cart-item{{display:flex;justify-content:space-between;gap:12px;align-items:center;border-bottom:1px solid var(--line);padding:14px 0}}.cart-item small{{display:block}}.cart-qty{{display:flex;align-items:center;gap:8px;width:135px}}.cart-qty input{{width:95px}}.sub-check{{font-size:12px;display:flex;align-items:center;gap:6px}}.sub-check input{{width:auto}}.checkout-layout{{display:grid;grid-template-columns:minmax(0,2fr) minmax(230px,1fr);gap:18px}}.checkout-layout aside{{align-self:start;position:sticky;top:95px}}@media(max-width:850px){{nav{{display:none}}.site-header{{padding:0 16px}}.hero{{grid-template-columns:1fr}}.hero-copy{{padding:38px 25px}}.hero-photo{{min-height:290px}}.category-grid{{grid-template-columns:repeat(2,1fr)}}.product-grid{{grid-template-columns:repeat(2,1fr)}}.trust-row{{grid-template-columns:1fr}}.how-grid{{grid-template-columns:1fr}}.checkout-layout{{grid-template-columns:1fr}}.checkout-layout aside{{position:static}}}}@media(max-width:480px){{.identity{{display:none}}main{{padding:0 14px}}.category-grid{{grid-template-columns:1fr 1fr}}.product-grid{{grid-template-columns:1fr 1fr;gap:9px}}.product-card img{{height:120px}}.product-info{{padding:10px}}}}
</style></head><body>{nav}{message}<main>{body}</main><footer class="site-footer"><span>FarmFresh · Fresh from local farms</span><span>Good food, delivered with care.</span></footer></body></html>''')


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db_pool


def csrf_for(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, form) -> None:
    expected = request.session.get("csrf")
    supplied = form.get("csrf", "")
    if not expected or not secrets.compare_digest(expected, str(supplied)):
        raise HTTPException(403, "Invalid form token; reload and retry")


def login_view(request: Request, notice: str = "") -> HTMLResponse:
    csrf = csrf_for(request)
    body = f'''<div class="form-narrow"><p class="eyebrow">Your fresh food account</p><h1 style="font-size:38px">Welcome to FarmFresh</h1><p class="muted">Sign in with the email and phone number registered to your account. Phone format: +911234567890.</p>{f'<p class="error">{esc(notice)}</p>' if notice else ''}<section><h2>Sign in</h2><form method="post" action="/ui/login"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>Email</label><input required type="email" name="email"><label>Phone number</label><input required type="tel" name="phone" placeholder="+911234567890"><button class="primary">Continue</button></form></section><section><h2>New to FarmFresh?</h2><p>Create a customer account to place your order.</p><form method="post" action="/ui/register"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>Your name</label><input required name="full_name" maxlength="120"><label>Email</label><input required type="email" name="email"><label>Phone number</label><input required type="tel" name="phone" maxlength="16" placeholder="+911234567890"><button class="primary">Create account</button></form></section><p class="muted">Farmer, vendor, and driver accounts are added by the platform owner. <a href="/ui/setup">First time platform owner?</a></p><p><a href="/ui">← Continue browsing</a></p></div>'''
    return page("Sign in", body, cart_count=len(request.session.get("cart", {})))


async def issue_challenge(request: Request, user_id: UUID) -> None:
    if os.getenv("APP_ENV") == "production" and not os.getenv("SESSION_SECRET"):
        raise HTTPException(503, "Configure SESSION_SECRET before enabling production sign-in")
    code = f"{secrets.randbelow(1_000_000):06d}"
    pool = get_pool(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM login_challenges WHERE created_at < now()-interval '30 days'")
        recent = await conn.fetchval("SELECT count(*) FROM login_challenges WHERE user_id=$1 AND created_at>now()-interval '10 minutes'", user_id)
        if recent >= 3:
            raise HTTPException(429, "Too many login codes requested. Wait ten minutes and try again.")
        await conn.execute("UPDATE login_challenges SET consumed_at=now() WHERE user_id=$1 AND consumed_at IS NULL", user_id)
        await conn.execute("INSERT INTO login_challenges(user_id,code_hash,expires_at) VALUES($1,$2,now()+interval '10 minutes')", user_id, otp_digest(code))
    user = await pool.fetchrow("SELECT email,phone FROM platform_users WHERE id=$1", user_id)
    smtp_host = os.getenv("SMTP_HOST")
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    if smtp_host and twilio_sid and os.getenv("SMTP_FROM") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_FROM"):
        await asyncio.to_thread(_deliver_codes, user["email"], user["phone"], code)
    elif os.getenv("APP_ENV", "development") == "development":
        log.warning("LOCAL DEVELOPMENT OTP for user %s: %s (valid 10 minutes)", user_id, code)
    else:
        raise HTTPException(503, "Configure SMTP and SMS credentials before enabling sign-in outside development")
    request.session["pending_user"] = str(user_id)


def _deliver_codes(email: str, phone: str, code: str) -> None:
    message = EmailMessage()
    message["Subject"] = "Fresh Produce sign-in code"
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = email
    message.set_content(f"Your Fresh Produce sign-in code is {code}. It expires in 10 minutes.")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=10) as client:
        client.starttls()
        if os.getenv("SMTP_USERNAME"):
            client.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        client.send_message(message)
    data = urllib.parse.urlencode({"To": phone, "From": os.environ["TWILIO_FROM"], "Body": f"Fresh Produce sign-in code: {code}. Expires in 10 minutes."}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{os.environ['TWILIO_ACCOUNT_SID']}/Messages.json", data=data)
    import base64
    auth = base64.b64encode(f"{os.environ['TWILIO_ACCOUNT_SID']}:{os.environ['TWILIO_AUTH_TOKEN']}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError("SMS provider rejected the login code")


def code_view(request: Request, message: str = "Enter the six-digit code printed in the API server terminal.") -> HTMLResponse:
    csrf = csrf_for(request)
    return page("Verify sign-in", f'''<h1>Verify your sign-in</h1><section><p>{esc(message)}</p><form method="post" action="/ui/verify"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>One-time code</label><input required name="code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6"><button>Verify and continue</button></form><p class="muted">Code expires after 10 minutes.</p></section>''')


@router.get("/ui", response_class=HTMLResponse)
async def home(request: Request):
    user=await current_user(request)
    async with get_pool(request).acquire() as conn:
        products=await conn.fetch("""SELECT p.id,p.name,p.category,p.default_unit,
            coalesce(sum(o.available_quantity) FILTER(WHERE o.status='available' AND o.harvest_date>=CURRENT_DATE AND EXISTS(SELECT 1 FROM platform_users u WHERE u.farm_id=o.farm_id AND u.role IN ('farmer','vendor') AND u.is_active)),0)::numeric stock,
            min(o.unit_price_paise) FILTER(WHERE o.status='available' AND o.harvest_date>=CURRENT_DATE AND o.available_quantity>0 AND EXISTS(SELECT 1 FROM platform_users u WHERE u.farm_id=o.farm_id AND u.role IN ('farmer','vendor') AND u.is_active)) price_paise,
            (SELECT fp.id FROM produce_offers po JOIN farm_photos fp ON fp.farm_id=po.farm_id WHERE po.product_id=p.id AND po.status='available' ORDER BY fp.created_at DESC LIMIT 1) photo_id
            FROM products p LEFT JOIN produce_offers o ON o.product_id=p.id WHERE p.status='active' GROUP BY p.id ORDER BY p.name LIMIT 16""")
    csrf=csrf_for(request)
    def asset(category):
        category=str(category or '').lower()
        if any(word in category for word in ('fruit','apple','berry')): return 'fruits.jpg'
        if any(word in category for word in ('dairy','egg','milk')): return 'dairy.jpg'
        if any(word in category for word in ('grain','pulse','rice','wheat')): return 'grains.jpg'
        if any(word in category for word in ('pantry','oil','spice')): return 'pantry.jpg'
        return 'vegetables.jpg'
    categories=[('Vegetables','Fresh from the soil','vegetables.jpg'),('Fruits','Sweet & seasonal','fruits.jpg'),('Dairy & Eggs','Farm fresh','dairy.jpg'),('Grains & Pulses','Everyday goodness','grains.jpg'),('Pantry Staples','Kitchen essentials','pantry.jpg')]
    category_cards=''.join(f'<a class="category-card" href="#shop"><img src="/ui-assets/images/{img}" alt="{esc(name)}"><div><b>{esc(name)}</b><small>{esc(sub)}</small></div></a>' for name,sub,img in categories)
    product_cards=''
    for p in products:
        img=f'/ui/photos/{esc(p["photo_id"])}' if p['photo_id'] else f'/ui-assets/images/{asset(p["category"])}'
        stock=float(p['stock'] or 0)
        stock_badge=f'<span class="stock">{stock:g} {esc(p["default_unit"])} available</span>' if stock>0 else '<span class="stock">Ask your local farm</span>'
        price=f'<p class="price">From ₹{int(p["price_paise"])/100:.2f} / {esc(p["default_unit"])}</p>' if p['price_paise'] is not None else '<p class="price">Price confirmed by farm</p>'
        product_cards+=f'''<article class="product-card"><img src="{img}" alt="Fresh {esc(p["name"])}"><div class="product-info"><small>{esc(p["category"])}</small><h3>{esc(p["name"])}</h3>{price}<p>Freshly sourced · Sold by {esc(p["default_unit"])}</p>{stock_badge}<form class="add-row" method="post" action="/ui/cart/add"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="product_id" value="{esc(p["id"])}"><input type="number" name="quantity" min="0.001" step="0.001" value="1" aria-label="Quantity"><button>Add to bag</button></form></div></article>'''
    if not product_cards:
        product_cards='<div class="card"><b>We are setting up the farm shelves.</b><p>Fresh products will show here soon.</p></div>'
    body=f'''<section class="hero"><div class="hero-copy"><span class="eyebrow">Good food, closer to home</span><h1>Farm fresh.<br>Feel good.</h1><p>Seasonal produce from local farms, brought to your doorstep with care.</p><div class="hero-actions"><a class="primary" href="#shop">Shop fresh <span>→</span></a><a class="secondary" href="#categories">Explore categories</a></div><div class="trust-row"><span>🌿 Fresh from farms</span><span>🚚 Planned for your area</span><span>♻️ Thoughtfully sourced</span></div></div><div class="hero-photo"><img src="/ui-assets/images/produce-hero.jpg" alt="A colorful selection of fresh vegetables"><div class="hero-note">🌱 Picked with care</div></div></section><section id="categories"><div class="section-head"><span class="eyebrow">Shop by category</span><h2>Good things, grown nearby</h2><p class="muted">Choose what sounds good today.</p></div><div class="category-grid">{category_cards}</div></section><section id="shop"><div class="section-head"><span class="eyebrow">From local farms</span><h2>Fresh picks for your table</h2><p class="muted">Add what you need. Sign in when you are ready to order.</p></div><div class="product-grid">{product_cards}</div></section><section id="how"><div class="section-head"><span class="eyebrow">Simple from start to doorstep</span><h2>How FarmFresh works</h2></div><div class="how-grid"><div class="how-card"><b>1 · Pick your fresh food</b><p>Browse produce available through local farms.</p></div><div class="how-card"><b>2 · Add your delivery place</b><p>Share your location or enter an address and landmark.</p></div><div class="how-card"><b>3 · We plan the delivery</b><p>Our team coordinates farm supply and delivery routes.</p></div></div></section>'''
    count=len(request.session.get('cart',{}))
    return page("Farm fresh delivered",body,user,cart_count=count)


def session_cart(request: Request) -> dict[str, str]:
    cart=request.session.get('cart',{})
    return cart if isinstance(cart,dict) else {}


@router.post("/ui/cart/add")
async def cart_add(request: Request):
    form=await request.form(); check_csrf(request,form)
    try:
        product=UUID(str(form['product_id'])); quantity=Decimal(str(form['quantity']))
    except Exception: raise HTTPException(422,'Choose a valid product and quantity')
    if not valid_quantity(quantity): raise HTTPException(422,'Quantity must be positive and up to three decimal places')
    async with get_pool(request).acquire() as conn:
        exists=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM products WHERE id=$1 AND status='active')",product)
    if not exists: raise HTTPException(404,'Product not found')
    cart=session_cart(request)
    key=str(product)
    new_quantity=Decimal(cart.get(key,'0'))+quantity
    if key not in cart and len(cart)>=20: raise HTTPException(422,'Your bag can contain up to 20 products')
    if not valid_quantity(new_quantity): raise HTTPException(422,'That quantity is too large')
    cart[key]=str(new_quantity); request.session['cart']=cart
    return RedirectResponse('/ui/cart',303)


@router.get("/ui/cart",response_class=HTMLResponse)
async def cart_page(request: Request):
    user=await current_user(request); cart=session_cart(request); csrf=csrf_for(request)
    rows=[]
    if cart:
        async with get_pool(request).acquire() as conn:
            rows=await conn.fetch("SELECT id,name,default_unit FROM products WHERE id=ANY($1::uuid[]) AND status='active' ORDER BY name",[UUID(k) for k in cart])
    inputs=''.join(f'<div class="cart-item"><div><b>{esc(p["name"])}</b><small> · {esc(p["default_unit"])}</small></div><div class="cart-qty"><input aria-label="{esc(p["name"])} quantity" type="number" name="qty_{esc(p["id"])}" min="0" step="0.001" value="{esc(cart.get(str(p["id"]),"0"))}"><span>{esc(p["default_unit"])}</span></div></div>' for p in rows)
    if not rows:
        body='<div class="form-narrow"><section><h1 style="font-size:38px">Your bag is waiting</h1><p class="muted">Add something fresh from the shop.</p><a class="primary" href="/ui#shop">Browse produce</a></section></div>'
    else:
        body=f'''<div class="form-narrow"><span class="eyebrow">Your selections</span><h1 style="font-size:40px">Shopping bag</h1><section><form method="post" action="/ui/cart/update"><input type="hidden" name="csrf" value="{esc(csrf)}">{inputs}<button class="secondary">Update quantities</button></form><p class="muted">Prices are confirmed by the platform after farm sourcing.</p><a class="primary" href="/ui/checkout">Continue to delivery →</a></section></div>'''
    return page('Your bag',body,user,cart_count=len(cart))


@router.post("/ui/cart/update")
async def cart_update(request: Request):
    form=await request.form(); check_csrf(request,form); updated={}
    for product_id in session_cart(request):
        value=str(form.get(f'qty_{product_id}','0')).strip()
        try: qty=Decimal(value)
        except InvalidOperation: raise HTTPException(422,'Enter valid quantities')
        if qty==0: continue
        if not valid_quantity(qty): raise HTTPException(422,'Quantity must be positive and up to three decimal places')
        updated[product_id]=str(qty)
    request.session['cart']=updated
    return RedirectResponse('/ui/cart',303)


@router.get("/ui/checkout",response_class=HTMLResponse)
async def checkout(request: Request, notice: str = ""):
    user=await current_user(request)
    if not user:
        request.session['after_login']='checkout'
        return login_view(request,'Sign in or create a customer account to finish your order. Your bag will be kept.')
    if user['role']!='customer': return RedirectResponse('/ui/dashboard?notice=Customer+account+needed+to+checkout',303)
    cart=session_cart(request)
    if not cart: return RedirectResponse('/ui/cart',303)
    async with get_pool(request).acquire() as conn:
        products=await conn.fetch("SELECT id,name,default_unit FROM products WHERE id=ANY($1::uuid[]) AND status='active' ORDER BY name",[UUID(k) for k in cart])
    lines=''.join(f'<div class="cart-item"><div><b>{esc(p["name"])}</b><small> · {esc(cart.get(str(p["id"])))} {esc(p["default_unit"])}</small></div><input type="hidden" name="qty_{esc(p["id"])}" value="{esc(cart.get(str(p["id"])))}"><label class="sub-check"><input type="checkbox" name="substitute_{esc(p["id"])}"> Allow a similar replacement</label></div>' for p in products)
    csrf=esc(csrf_for(request))
    body=f'''<div class="checkout-layout"><div><span class="eyebrow">Almost there</span><h1 style="font-size:40px">Where should we bring it?</h1><section><form method="post" action="/ui/customer/order"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" id="latitude" name="lat"><input type="hidden" id="longitude" name="lon"><label>Delivery address or landmark</label><textarea id="delivery_address" name="delivery_address" rows="3" placeholder="Building, street, area, city, postal code or landmark"></textarea><div class="address-options"><button type="button" class="secondary" onclick="useMyLocation()">◎ Use my current location</button><span id="location_status" class="location-status">Type an address, or share location for an ETA.</span></div><p class="muted">Delivery time is planned from farm availability and route planning.</p><div id="basket_estimate" class="card muted">Share your location to see an estimated total and arrival time.</div><h3>Your items</h3>{lines}<button class="primary" type="submit">Place my order</button></form></section></div><aside class="card"><h2>Good choice.</h2><p>We’ll ask nearby vendors to confirm the items, then build the route and offer it to a driver.</p><a href="/ui/cart">Edit your bag</a></aside></div><script>async function updateEstimate(){{const lat=document.getElementById('latitude').value,lon=document.getElementById('longitude').value;if(!lat||!lon)return;const box=document.getElementById('basket_estimate');box.textContent='Calculating your estimate…';try{{const r=await fetch('/ui/estimate?lat='+encodeURIComponent(lat)+'&lon='+encodeURIComponent(lon));const q=await r.json();box.textContent=(q.total_paise===null?'Some items need vendor pricing. ':'Estimated total ₹'+(q.total_paise/100).toFixed(2)+'. ')+(q.eta_minutes?'Estimated delivery in about '+q.eta_minutes+' min if vendors confirm current availability. ':'')+(q.missing.length?'Supply check: '+q.missing.join('; '):q.provider)}}catch(e){{box.textContent='Estimate unavailable right now. You can still place an order for a vendor availability check.'}}}}function useMyLocation(){{const s=document.getElementById('location_status');if(!navigator.geolocation){{s.textContent='Location is not available in this browser. Please type your address.';return}}s.textContent='Finding your location…';navigator.geolocation.getCurrentPosition(p=>{{document.getElementById('latitude').value=p.coords.latitude;document.getElementById('longitude').value=p.coords.longitude;s.textContent='Location pin added. Add an apartment or landmark above if helpful.';updateEstimate()}},()=>{{s.textContent='We could not access your location. Please type your address.'}},{{enableHighAccuracy:true,timeout:10000}})}}</script>'''
    return page('Delivery details',body,user,notice=notice,cart_count=len(cart))


@router.get("/ui/estimate")
async def customer_estimate(request: Request, lat: float, lon: float):
    user=await current_user(request)
    if not user or user['role']!='customer': raise HTTPException(401,'Sign in required')
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
        raise HTTPException(422,'Invalid location')
    async with get_pool(request).acquire() as conn:
        quote=await quote_basket(conn,session_cart(request),lat,lon)
    return quote


@router.get("/ui/login", response_class=HTMLResponse)
async def login(request: Request):
    return login_view(request)


@router.get("/ui/setup", response_class=HTMLResponse)
async def first_owner_setup(request: Request, notice: str = ""):
    """Create the first operations account from the browser, without SQL."""
    pool = get_pool(request)
    if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM platform_users WHERE role='operations')"):
        return RedirectResponse('/ui/login', 303)
    csrf = esc(csrf_for(request))
    key_field = '<label>Owner setup key</label><input name="setup_key" type="password" required>' if os.getenv('APP_ENV') == 'production' else ''
    body = f'''<div class="form-narrow"><span class="eyebrow">First time setup</span><h1 style="font-size:40px">Create your platform owner</h1><p class="muted">This one-time account manages farms, products, and team access.</p>{f'<p class="error">{esc(notice)}</p>' if notice else ''}<section><form method="post" action="/ui/setup"><input type="hidden" name="csrf" value="{csrf}"><label>Your name</label><input name="full_name" maxlength="120" required><label>Email</label><input name="email" type="email" required><label>Phone in international format</label><input name="phone" type="tel" placeholder="+911234567890" required>{key_field}<button>Create owner account</button></form></section><p><a href="/ui">← Back to the shop</a></p></div>'''
    return page('Platform setup', body)


@router.post("/ui/setup")
async def create_first_owner(request: Request):
    form = await request.form(); check_csrf(request, form)
    name = str(form.get('full_name', '')).strip()[:120]
    email = str(form.get('email', '')).strip().lower()
    phone = str(form.get('phone', '')).strip()
    setup_key = str(form.get('setup_key', ''))
    expected_key = os.getenv('OWNER_SETUP_KEY')
    if os.getenv('APP_ENV') == 'production' and (not expected_key or not secrets.compare_digest(setup_key, expected_key)):
        return await first_owner_setup(request, 'The owner setup key is missing or incorrect. Configure OWNER_SETUP_KEY and try again.')
    if not name or '@' not in email or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        return await first_owner_setup(request, 'Enter your name, email, and phone in international format.')
    pool = get_pool(request)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(641932701)")
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM platform_users WHERE role='operations')"):
                    return RedirectResponse('/ui/login?notice=Owner+setup+is+already+complete', 303)
                row = await conn.fetchrow("INSERT INTO platform_users(full_name,email,phone,role) VALUES($1,$2,$3,'operations') RETURNING id", name, email, phone)
    except asyncpg.UniqueViolationError:
        return await first_owner_setup(request, 'That email or phone is already registered.')
    await issue_challenge(request, row['id'])
    return code_view(request, 'Owner account created. Enter the six-digit code printed in the API server terminal.')


@router.post("/ui/login")
async def start_login(request: Request):
    form = await request.form(); check_csrf(request, form)
    email = str(form.get("email", "")).strip().lower(); phone = str(form.get("phone", "")).strip()
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        return login_view(request, "Enter the phone number in international format, for example +911234567890.")
    async with get_pool(request).acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM platform_users WHERE lower(email)=$1 AND phone=$2 AND is_active", email, phone)
    if user is None:
        return login_view(request, "No active account matches that email and phone. Check the details or create a customer account.")
    await issue_challenge(request, user["id"])
    return code_view(request)


@router.post("/ui/register")
async def register(request: Request):
    form = await request.form(); check_csrf(request, form)
    name = str(form.get("full_name", "")).strip()[:120]
    email = str(form.get("email", "")).strip().lower()
    phone = str(form.get("phone", "")).strip()[:32]
    if not name or "@" not in email or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        return login_view(request, "Enter your name, valid email, and phone in international format such as +911234567890.")
    async with get_pool(request).acquire() as conn:
        try:
            row = await conn.fetchrow("INSERT INTO platform_users(full_name,email,phone,role) VALUES($1,$2,$3,'customer') RETURNING id", name, email, phone)
        except asyncpg.UniqueViolationError:
            return login_view(request, "An account already uses that email or phone. Sign in instead.")
    await issue_challenge(request, row["id"])
    return code_view(request, "Account created. Enter the six-digit code printed in the API server terminal.")


@router.post("/ui/verify")
async def verify(request: Request):
    form = await request.form(); check_csrf(request, form)
    user_id = request.session.get("pending_user"); code = str(form.get("code", ""))
    if not user_id or len(code) != 6 or not code.isdigit():
        return code_view(request, "Invalid or expired verification request. Start again.")
    pool = get_pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            challenge = await conn.fetchrow("SELECT id,code_hash,expires_at,attempts FROM login_challenges WHERE user_id=$1 AND consumed_at IS NULL ORDER BY created_at DESC LIMIT 1 FOR UPDATE", UUID(user_id))
            if challenge is None or challenge["expires_at"] < datetime.now(timezone.utc) or challenge["attempts"] >= 5:
                return login_view(request, "Code expired or attempt limit reached. Sign in again.")
            digest = otp_digest(code)
            if not secrets.compare_digest(digest, challenge["code_hash"]):
                await conn.execute("UPDATE login_challenges SET attempts=attempts+1 WHERE id=$1", challenge["id"])
                return code_view(request, "That code is incorrect. Try again; five attempts are allowed.")
            user = await conn.fetchrow("SELECT id,full_name,email,phone,role,farm_id FROM platform_users WHERE id=$1 AND is_active", UUID(user_id))
            if user is None:
                return login_view(request, "Account is inactive.")
            await conn.execute("UPDATE login_challenges SET consumed_at=now() WHERE id=$1", challenge["id"])
    cart=request.session.get("cart",{})
    after_login=request.session.get("after_login")
    request.session.clear()
    if cart: request.session["cart"]=cart
    if after_login: request.session["after_login"]=after_login
    request.session["user_id"] = str(user["id"])
    request.session["csrf"] = secrets.token_urlsafe(24)
    return RedirectResponse("/ui/checkout" if after_login else "/ui/dashboard", status_code=303)


async def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    async with get_pool(request).acquire() as conn:
        row = await conn.fetchrow("SELECT id,full_name,email,phone,role,farm_id FROM platform_users WHERE id=$1 AND is_active", UUID(user_id))
    if not row:
        request.session.clear(); return None
    data = dict(row); data["csrf"] = csrf_for(request)
    return data


def form_csrf(user):
    return f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'


def table(rows, columns):
    if not rows: return '<p class="muted">No records yet.</p>'
    head = ''.join(f'<th>{esc(label)}</th>' for _, label in columns)
    body = ''.join('<tr>'+''.join(f'<td>{esc(row.get(key))}</td>' for key,_ in columns)+'</tr>' for row in rows)
    return f'<div style="overflow:auto"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


@router.get("/ui/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, notice: str = ""):
    user = await current_user(request)
    if not user: return RedirectResponse("/ui/login", status_code=303)
    pool = get_pool(request)
    async with pool.acquire() as conn:
        if user["role"] == "customer":
            orders = await conn.fetch("SELECT id,status,delivery_address,estimated_total_paise,estimated_delivery_minutes,created_at FROM orders WHERE customer_id=$1 ORDER BY created_at DESC LIMIT 20", user["id"])
            order_cards=''
            for o in orders:
                cancel=''
                if o['status'] in {'received','sourcing','confirmed'}:
                    cancel=f'<form method="post" action="/ui/customer/cancel">{form_csrf(user)}<input type="hidden" name="id" value="{esc(o["id"])}"><button class="secondary">Cancel</button></form>'
                price=f'₹{int(o["estimated_total_paise"])/100:.2f} estimated' if o['estimated_total_paise'] is not None else 'Price confirmed after vendor response'
                eta=f'About {int(o["estimated_delivery_minutes"])} minutes' if o['estimated_delivery_minutes'] is not None else 'Arrival estimate after location and route are confirmed'
                order_cards+=f'<article class="card order-card"><div><small>Order · {esc(str(o["id"])[-8:])}</small><h3>{esc(o["status"].replace("_"," ").title())}</h3><p>{esc(o["delivery_address"] or "Delivery place pending")}</p><b>{price}</b><p>{eta}</p><small>{esc(o["created_at"])}</small></div>{cancel}</article>'
            if not order_cards: order_cards='<p class="muted">No orders yet. Your bag is ready when you are.</p>'
            body='<div class="section-head"><span class="eyebrow">Your account</span><h1 style="font-size:40px">My orders</h1><p class="muted">Our team plans delivery timing after sourcing and route planning.</p><a class="primary" href="/ui#shop">Shop fresh →</a></div><section><h2>Order history</h2>'+order_cards+'</section>'
        elif user["role"] in {"farmer","vendor"}:
            if not user["farm_id"]:
                body='<h1>Farmer dashboard</h1><p class="error">This account is not linked to a farm yet. Ask an operations user to link your account using platform_users.farm_id.</p>'
            else:
                farm=await conn.fetchrow("SELECT id,name,address,status FROM farms WHERE id=$1",user["farm_id"])
                parcels=await conn.fetch("SELECT id,name,round(ST_Area(shape::geography)::numeric,1) area_sq_m FROM land_parcels WHERE farm_id=$1 ORDER BY name",user["farm_id"])
                products=await conn.fetch("SELECT id,name,default_unit FROM products WHERE status='active' ORDER BY name")
                product_opts=''.join(f'<option value="{esc(p["id"])}" data-unit="{esc(p["default_unit"])}">{esc(p["name"])} ({esc(p["default_unit"])})</option>' for p in products)
                offers=await conn.fetch("SELECT o.id,p.name,o.available_quantity,o.unit,o.unit_price_paise,o.harvest_date,o.quality_grade,o.status FROM produce_offers o JOIN products p ON p.id=o.product_id WHERE o.farm_id=$1 ORDER BY o.harvest_date DESC LIMIT 100",user["farm_id"])
                commits=await conn.fetch("SELECT vc.id,vc.order_id,p.name product_name,vc.delivery_date,vc.requested_quantity,vc.unit,vc.ready_by,vc.status FROM vendor_commitments vc JOIN products p ON p.id=vc.product_id WHERE vc.farm_id=$1 ORDER BY vc.created_at DESC LIMIT 50",user["farm_id"])
                photos=await conn.fetch("SELECT id,caption,created_at FROM farm_photos WHERE farm_id=$1 ORDER BY created_at DESC LIMIT 20",user["farm_id"])
                photo_list=''.join(f'<li><a href="/ui/photos/{esc(p["id"])}">{esc(p["caption"] or "Farm photo")}</a> · {esc(p["created_at"])}</li>' for p in photos) or '<li>No photos uploaded yet.</li>'
                offer_cards=''
                for o in offers:
                    withdraw=''
                    if o['status']=='available':
                        withdraw=f'<form method="post" action="/ui/farmer/withdraw">{form_csrf(user)}<input type="hidden" name="id" value="{esc(o["id"])}"><button class="secondary">Pause this offer</button></form>'
                    offer_cards+=f'<article class="card"><h3>{esc(o["name"])}</h3><p>{esc(o["available_quantity"])} {esc(o["unit"])} · harvest {esc(o["harvest_date"])}</p><span class="stock">₹{int(o["unit_price_paise"])/100:.2f} / {esc(o["unit"])}</span><p>{esc(o["status"].title())}</p>{withdraw}</article>'
                if not offer_cards: offer_cards='<p>No offers yet. Add your first fresh item below.</p>'
                commitment_cards=''
                for c in commits:
                    response=''
                    if c['status']=='requested':
                        response=f'<form method="post" action="/ui/farmer/commitment">{form_csrf(user)}<input type="hidden" name="id" value="{esc(c["id"])}"><label>Your response</label><select name="status"><option value="confirmed">I can provide this</option><option value="shortfall">I can provide less</option><option value="rejected">I cannot provide it</option></select><label>Quantity you can provide</label><input name="quantity" type="number" min="0" step="0.001"><label>When will it be ready?</label><input name="ready_at" type="datetime-local"><button>Send response</button></form>'
                    commitment_cards+=f'<article class="card"><small>{"Customer order · "+esc(str(c["order_id"])[-8:]) if c["order_id"] else "Platform request"}</small><h3>{esc(c["product_name"])}</h3><p>{esc(c["requested_quantity"])} {esc(c["unit"])} requested · needed {esc(c["ready_by"])}</p><b>{esc(c["status"].title())}</b>{response}</article>'
                if not commitment_cards: commitment_cards='<p class="muted">No requests for this farm yet. Requests are sent to the vendor account linked to the farm that has the matching available offer. Ask the platform owner to check this account’s linked farm and the customer order’s supplier farm.</p>'
                body = (
                    '<div class="section-head"><span class="eyebrow">Your farm space</span><h1 style="font-size:40px">Hello, ' + esc(user["full_name"].split()[0]) + '</h1><p>Manage your harvest and farm requests.</p></div><section><h2>Farm profile</h2>' + table([dict(farm)] if farm else [], [('name','Farm'),('address','Address'),('status','Status')]) + '<h3>Your land</h3>' + table([dict(r) for r in parcels], [('name','Parcel'),('area_sq_m','Area m²')]) + '</section>'
                    '<section><h2>Your produce</h2><div class="grid">' + offer_cards + '</div>' +
                    '</section><section><h2>Requests from FarmFresh</h2><div class="grid">' + commitment_cards + '</div></section><section><h2>Upload farm photo</h2><form method="post" enctype="multipart/form-data" action="/ui/farmer/photo">' + form_csrf(user) +
                    '<label>Image (JPEG, PNG, WebP; max 8 MiB)</label><input name="file" type="file" accept="image/jpeg,image/png,image/webp" required>'
                    '<label>Caption</label><input name="caption" maxlength="240"><button>Upload</button></form><h3>Farm gallery</h3><ul>' + photo_list + '</ul></section>'
                    '<section><h2>Add a produce offer</h2><p class="muted">Set your price and available stock. Orders will request confirmation automatically.</p><form method="post" action="/ui/farmer/offer">' + form_csrf(user) +
                    f'<label>Product</label><select name="product_id" required>{product_opts}</select><label>Available quantity (in the product unit)</label><input type="number" name="quantity" min="0.001" step="0.001" required>'
                    '<label>Price per unit (₹)</label><input name="price" type="number" min="0.01" step="0.01" required><label>Harvest date</label><input name="harvest_date" type="date" required>'
                    '<label>Ready at (optional)</label><input name="ready_at" type="datetime-local"><button>Publish offer</button></form></section>'
                    '<section><h2>Add a new product</h2><form method="post" action="/ui/farmer/product">' + form_csrf(user) +
                    '<label>Product name</label><input name="name" maxlength="120" required><label>Category</label><input name="category" maxlength="80" required><label>Unit</label><input name="unit" value="kg" maxlength="24" required><label>Available quantity</label><input name="quantity" type="number" min="0.001" step="0.001" required><label>Price per unit (₹)</label><input name="price" type="number" min="0.01" step="0.01" required><label>Harvest date</label><input name="harvest_date" type="date" required><button>Add product and publish offer</button></form></section>'
                )
        elif user["role"] == "driver":
            assigns=await conn.fetch("SELECT a.id assignment_id,a.task_id,a.status assignment_status,a.compensation_cents,a.created_at,s.planning_job_id,s.vehicle_id FROM driver_task_assignments a JOIN route_stops s ON s.task_id=a.task_id WHERE a.driver_id=$1 ORDER BY a.created_at DESC LIMIT 50",user["id"])
            assignment_cards=''
            for assignment in assigns:
                stops=await conn.fetch("SELECT id stop_id,stop_type,sequence_number,order_id,arrival_minute,ST_Y(location::geometry) latitude,ST_X(location::geometry) longitude,status FROM route_stops WHERE planning_job_id=$1 AND vehicle_id=$2 ORDER BY sequence_number",assignment['planning_job_id'],assignment['vehicle_id'])
                stop_list=''
                for stop in stops:
                    lat=esc(stop['latitude']); lon=esc(stop['longitude'])
                    map_link=f'<a class="secondary" target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}">Map</a>'
                    event_form=''
                    if assignment['assignment_status']=='accepted':
                        event_form=f'<form method="post" action="/ui/driver/event">{form_csrf(user)}<input type="hidden" name="id" value="{esc(stop["stop_id"])}"><label>Update this stop</label><select name="event"><option value="arrived">Arrived</option><option value="picked_up">Picked up produce</option><option value="delivered">Delivered</option><option value="failed">Could not complete</option><option value="quality_rejected">Quality issue</option></select><button>Save update</button></form>'
                    stop_list+=f'<article class="how-card"><b>Stop {esc(stop["sequence_number"]+1)} · {esc(stop["stop_type"].title())}</b><p>Order {esc(str(stop["order_id"])[-8:] if stop["order_id"] else "Farm pickup")} · about {esc(stop["arrival_minute"])} min</p><p>Status: {esc(stop["status"].replace("_"," ").title())} {map_link}</p>{event_form}</article>'
                response_form=''
                if assignment['assignment_status']=='offered':
                    response_form=f'<form method="post" action="/ui/driver/response">{form_csrf(user)}<input type="hidden" name="id" value="{esc(assignment["assignment_id"])}"><button name="status" value="accepted">Accept this route</button><button class="secondary" name="status" value="declined">Can’t take it</button></form>'
                assignment_cards+=f'<article class="card"><small>Route offer · {esc(str(assignment["assignment_id"])[-8:])}</small><h3>₹{int(assignment["compensation_cents"])/100:.2f} estimated pay</h3><p>Status: {esc(assignment["assignment_status"].replace("_"," ").title())}</p><div class="grid">{stop_list}</div>{response_form}</article>'
            if not assignment_cards: assignment_cards='<p class="muted">No delivery jobs yet. New offers will show here.</p>'
            body = (
                '<div class="section-head"><span class="eyebrow">Your delivery day</span><h1 style="font-size:40px">Driver jobs</h1><p>Accept a job, open the stop map, and tap what happened.</p></div><section><div class="grid">' + assignment_cards + '</div></section>'
            )
        else:
            counts=await conn.fetch("SELECT status,count(*)::int count FROM orders GROUP BY status ORDER BY status")
            orders=await conn.fetch("""SELECT o.id,o.status,o.delivery_address,o.estimated_total_paise,o.estimated_delivery_minutes,o.created_at,(o.delivery_point IS NOT NULL) location_pinned,
                (SELECT string_agg(p.name||' '||ol.quantity||' '||ol.unit,', ' ORDER BY p.name) FROM order_lines ol JOIN products p ON p.id=ol.product_id WHERE ol.order_id=o.id) items,
                (SELECT count(*)::int FROM vendor_commitments vc WHERE vc.order_id=o.id) vendor_count,
                (SELECT count(*)::int FROM vendor_commitments vc WHERE vc.order_id=o.id AND vc.status='confirmed') confirmed_count,
                (SELECT string_agg(f.name||': '||p.name||' ('||vc.status||')',', ' ORDER BY f.name,p.name)
                 FROM vendor_commitments vc JOIN farms f ON f.id=vc.farm_id JOIN products p ON p.id=vc.product_id
                 WHERE vc.order_id=o.id) supplier_summary,
                (SELECT string_agg(u.full_name||' · ₹'||to_char(a.compensation_cents/100.0,'FM999999990.00')||' · '||a.status,', ' ORDER BY a.created_at)
                 FROM driver_task_assignments a JOIN route_stops rs ON rs.task_id=a.task_id JOIN platform_users u ON u.id=a.driver_id
                 WHERE rs.order_id=o.id) driver_offers,
                pj.status planning_status,pj.error planning_error
                FROM orders o LEFT JOIN planning_jobs pj ON pj.id=o.planning_job_id ORDER BY o.created_at DESC LIMIT 50""")
            jobs=await conn.fetch("SELECT id,status,error,created_at FROM planning_jobs ORDER BY created_at DESC LIMIT 20")
            farms=await conn.fetch("SELECT id,name FROM farms WHERE status='active' ORDER BY name LIMIT 100")
            offers=await conn.fetch("SELECT o.id,f.name farm,p.name product,o.available_quantity,o.unit,o.status FROM produce_offers o JOIN farms f ON f.id=o.farm_id JOIN products p ON p.id=o.product_id WHERE o.status='available' ORDER BY o.harvest_date LIMIT 50")
            demand=await conn.fetch("WITH daily AS (SELECT ol.product_id,ol.unit,date_trunc('day',o.created_at)::date d,sum(ol.quantity) qty FROM orders o JOIN order_lines ol ON ol.order_id=o.id WHERE o.status<>'cancelled' AND o.created_at>=now()-interval '28 days' GROUP BY ol.product_id,ol.unit,date_trunc('day',o.created_at)::date), pairs AS (SELECT DISTINCT product_id,unit FROM daily), calendar AS (SELECT generate_series(CURRENT_DATE-27,CURRENT_DATE,interval '1 day')::date d) SELECT p.name,pair.unit,round(avg(coalesce(d.qty,0))::numeric,2) daily_mean,round(stddev_pop(coalesce(d.qty,0))::numeric,2) daily_variability FROM pairs pair CROSS JOIN calendar c JOIN products p ON p.id=pair.product_id LEFT JOIN daily d ON d.product_id=pair.product_id AND d.unit=pair.unit AND d.d=c.d GROUP BY p.name,pair.unit ORDER BY daily_mean DESC LIMIT 20")
            upcoming=await conn.fetch("SELECT p.name,vc.status,count(*)::int requests,coalesce(sum(vc.confirmed_quantity),0)::numeric confirmed_qty FROM vendor_commitments vc JOIN products p ON p.id=vc.product_id WHERE vc.delivery_date BETWEEN CURRENT_DATE AND CURRENT_DATE+7 GROUP BY p.name,vc.status ORDER BY p.name")
            route_counts=await conn.fetch("SELECT status,count(*)::int stop_count FROM route_stops WHERE updated_at>=now()-interval '7 days' GROUP BY status ORDER BY status")
            users=await conn.fetch("SELECT u.id,u.full_name,u.email,u.phone,u.role,u.is_active,u.farm_id,f.name farm_name FROM platform_users u LEFT JOIN farms f ON f.id=u.farm_id ORDER BY u.created_at DESC LIMIT 50")
            insights=''.join(f'<div class="card"><b>{esc(r["status"])}</b><br>{r["count"]} orders</div>' for r in counts)
            farm_opts=''.join(f'<option value="{esc(f["id"])}">{esc(f["name"])}</option>' for f in farms)
            account_cards=''
            for account in users:
                farm_name=esc(account['farm_name'] or 'No farm linked')
                relink=''
                if account['role'] in {'farmer','vendor'}:
                    choices=''.join(f'<option value="{esc(f["id"])}" {"selected" if account["farm_id"]==f["id"] else ""}>{esc(f["name"])}</option>' for f in farms)
                    relink=f'<form method="post" action="/ui/operations/users/{esc(account["id"])}/farm">{form_csrf(user)}<label>Farm for {esc(account["role"])} account</label><select name="farm_id" required>{choices}</select><button class="secondary">Save farm link</button></form>'
                account_cards+=f'<article class="card"><h3>{esc(account["full_name"])}</h3><p>{esc(account["email"])} · {esc(account["phone"])}</p><p>{esc(account["role"].title())} · {farm_name} · {"Active" if account["is_active"] else "Inactive"}</p>{relink}</article>'
            if not account_cards: account_cards='<p>No accounts yet.</p>'
            order_cards=''
            for o in orders:
                price=f'₹{int(o["estimated_total_paise"])/100:.2f} estimated' if o['estimated_total_paise'] is not None else 'Price pending vendor offer'
                eta=f'~{int(o["estimated_delivery_minutes"])} min' if o['estimated_delivery_minutes'] is not None else 'ETA pending location/route'
                supply=f'{o["confirmed_count"]}/{o["vendor_count"]} vendors confirmed'
                supplier_summary=esc(o['supplier_summary']) if o['supplier_summary'] else 'No farm offers were reserved for this order'
                driver_summary=esc(o['driver_offers']) if o['driver_offers'] else 'Driver offer pending route planning'
                planning=(f'Route: {o["planning_status"]}' if o['planning_status'] else 'Route: waiting for vendor confirmations or a driver')
                if o['planning_error']: planning+=f' · {esc(o["planning_error"])}'
                pin_form=''
                if not o['location_pinned'] and o['delivery_address']:
                    pin_form=f'<form method="post" action="/ui/operations/order-location">{form_csrf(user)}<input type="hidden" name="id" value="{esc(o["id"])}"><label>Confirm delivery address</label><input name="address" value="{esc(o["delivery_address"])}" required><button class="secondary">Find address and continue routing</button></form>'
                order_cards+=f'<article class="card"><small>Order · {esc(str(o["id"])[-8:])} · {esc(o["created_at"])}</small><h3>{esc(o["status"].replace("_"," ").title())}</h3><p>{esc(o["items"] or "No line items")}</p><p>{esc(o["delivery_address"] or "Address pending")}</p><b>{price}</b><p>{eta} · {supply}</p><p>Supply requests: {supplier_summary}</p><p>Drivers: {driver_summary}</p><span class="stock">{esc(planning)}</span>{pin_form}</article>'
            if not order_cards: order_cards='<p>No customer orders yet.</p>'
            quality_cards=''
            for o in offers:
                quality_cards+=f'<article class="card"><small>{esc(o["farm"])}</small><h3>{esc(o["product"])}</h3><p>{esc(o["available_quantity"])} {esc(o["unit"])} · {esc(o["status"])}</p><form method="post" action="/ui/operations/quality">{form_csrf(user)}<input type="hidden" name="id" value="{esc(o["id"])}"><label>Grade</label><input name="grade" value="A" required><label>Inspector</label><input name="inspector" required><label>Result</label><select name="passed"><option value="true">Pass</option><option value="false">Reject</option></select><button>Save quality check</button></form></article>'
            if not quality_cards: quality_cards='<p>No available offers to inspect.</p>'
            body = (
                '<h1>Platform operations</h1><div class="grid">' + insights + '</div>'
                '<section><h2>Generate demo data</h2><form method="post" action="/ui/operations/simulate">' + form_csrf(user) +
                '<label>Farms</label><input name="farms" type="number" value="5" min="1" max="100">'
                '<label>Offers per farm</label><input name="offers" type="number" value="3" min="1" max="20">'
                '<label>Seed</label><input name="seed" type="number" value="42"><button>Generate</button></form></section>'
                '<section><h2>Orders to handle</h2><div class="grid">' + order_cards + '</div></section>'
                '<section><h2>Automatic fulfillment</h2><p>Orders reserve available vendor offers. Vendor confirmations queue route planning. A completed route receives an estimated driver pay offer automatically.</p>' + table([dict(r) for r in jobs], [('status','Route status'),('error','Attention needed'),('created_at','Created')]) + '</section>'
                '<section><h2>Demand baseline - trailing 28 days</h2><p class="muted">Daily mean includes zero-order days; variability is standard deviation, not a trained forecast.</p>' + table([dict(r) for r in demand], [('name','Produce'),('unit','Unit'),('daily_mean','Daily mean'),('daily_variability','Variability')]) + '<h3>Vendor commitments due in 7 days</h3>' + table([dict(r) for r in upcoming], [('name','Produce'),('status','Status'),('requests','Requests'),('confirmed_qty','Confirmed quantity')]) + '<h3>Route stops updated in 7 days</h3>' + table([dict(r) for r in route_counts], [('status','Status'),('stop_count','Stops')]) + '</section>'
                '<section><h2>Inspect fresh offers</h2><div class="grid">' + quality_cards + '</div></section>'
                '<section><h2>Set up your catalog</h2><p class="muted">Add your first farm and products here. You can then link farmer accounts and publish produce.</p><div class="grid"><form class="card" method="post" action="/ui/operations/farm">' + form_csrf(user) +
                '<h3>Add a farm</h3><label>Farm name</label><input name="name" maxlength="160" required><label>Farm address</label><textarea name="address" maxlength="500" required placeholder="Village, road, district, state"></textarea><input type="hidden" name="lat" id="farm_lat"><input type="hidden" name="lon" id="farm_lon"><button type="button" class="secondary" onclick="farmLocation()">Use this device’s farm location</button><span id="farm_location_status" class="location-status">Or enter an address. Address lookup uses GEOCODER_URL if configured.</span><button>Create farm</button><script>function farmLocation(){{navigator.geolocation.getCurrentPosition(p=>{{document.getElementById("farm_lat").value=p.coords.latitude;document.getElementById("farm_lon").value=p.coords.longitude;document.getElementById("farm_location_status").textContent="Farm location saved."}},()=>{{document.getElementById("farm_location_status").textContent="Could not access location; enter a geocodable address."}},{{enableHighAccuracy:true,timeout:10000}})}}</script></form>'
                '<form class="card" method="post" action="/ui/operations/product">' + form_csrf(user) +
                '<h3>Add a product</h3><label>Product name</label><input name="name" maxlength="120" required><label>Category</label><input name="category" maxlength="80" placeholder="Vegetable, fruit, grains…" required><label>Unit</label><input name="unit" maxlength="24" placeholder="kg" required><label>Max transit time (minutes)</label><input name="transit" type="number" min="1" value="240" required><button>Create product</button></form></div></section>'
                '<section><h2>Accounts and farm links</h2><p class="muted">Customer orders create supply requests for the farm with the matching offer. Link each farmer or vendor account to that same farm to show its requests.</p><div class="grid">' + account_cards + '</div>' +
                '<h3>Add an account</h3>' +
                '<form method="post" action="/ui/operations/users">' + form_csrf(user) +
                '<label>Full name</label><input name="full_name" required maxlength="120"><label>Email</label><input name="email" type="email" required>'
                '<label>Phone in international format</label><input name="phone" placeholder="+911234567890" required>'
                '<label>Role</label><select name="role"><option value="farmer">Farmer</option><option value="vendor">Vendor</option><option value="driver">Driver</option><option value="operations">Operations</option></select>'
                f'<label>Link farm (required for farmer or vendor)</label><select name="farm_id"><option value="">Choose a farm</option>{farm_opts}</select><button>Create account</button></form></section>'
            )
    return page("Dashboard", body, user, notice)


@router.get("/ui/search", response_class=HTMLResponse)
async def search(request: Request, product_id: UUID, quantity: float, lat: float, lon: float):
    user=await current_user(request)
    if not user or user["role"]!="customer": return RedirectResponse("/ui/login",303)
    if not math.isfinite(quantity) or quantity<=0 or not -90<=lat<=90 or not -180<=lon<=180:
        raise HTTPException(422,"Enter a positive quantity and valid coordinates")
    async with get_pool(request).acquire() as conn:
        rows=await conn.fetch("""WITH origin AS(SELECT ST_SetSRID(ST_MakePoint($1,$2),4326)::geography p) SELECT f.name farm,o.id offer_id,o.available_quantity,o.unit,o.harvest_date,o.quality_grade,ST_Distance(f.location,origin.p)::int distance_m,(SELECT id FROM farm_photos WHERE farm_id=f.id ORDER BY created_at DESC LIMIT 1) photo_id FROM farms f JOIN produce_offers o ON o.farm_id=f.id CROSS JOIN origin WHERE f.status='active' AND o.status='available' AND o.harvest_date>=CURRENT_DATE AND o.product_id=$3 AND o.available_quantity >= $4 AND ST_DWithin(f.location,origin.p,25000) ORDER BY distance_m LIMIT 50""",lon,lat,product_id,quantity)
    cards = ''
    for r in rows:
        photo = f'<img alt="Farm photo" style="max-width:220px;max-height:150px" src="/ui/photos/{esc(r["photo_id"])}">' if r["photo_id"] else ''
        cards += f'<article class="card">{photo}<h2>{esc(r["farm"])}</h2><p>{esc(r["available_quantity"])} {esc(r["unit"])} · harvest {esc(r["harvest_date"])} · {esc(r["distance_m"])} m away</p><p>Quality grade: {esc(r["quality_grade"] or "Not checked")}</p></article>'
    if not cards:
        cards = '<p>No nearby farms matched this product and quantity.</p>'
    body='<h1>Nearby produce</h1><div class="grid">'+cards+'</div><p><a href="/ui/dashboard">Back to dashboard</a></p>'
    return page("Farm search",body,user)


@router.get("/ui/photos/{photo_id}")
async def ui_photo(request: Request, photo_id: UUID):
    from fastapi.responses import Response
    user=await current_user(request)
    if not user: return RedirectResponse('/ui/login',303)
    async with get_pool(request).acquire() as conn:
        row=await conn.fetchrow("SELECT content_type,data FROM farm_photos WHERE id=$1",photo_id)
    if not row: raise HTTPException(404,'Photo not found')
    return Response(bytes(row['data']),media_type=row['content_type'],headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})


async def form_user(request, form, role=None):
    check_csrf(request,form); user=await current_user(request)
    if not user: raise HTTPException(401,"Sign in required")
    if role and user["role"] not in role: raise HTTPException(403,"Not authorized")
    return user


@router.post("/ui/customer/order")
async def customer_order(request: Request):
    form=await request.form(); user=await form_user(request,form,{"customer"})
    try:
        lat=float(form['lat']) if form.get('lat') else None
        lon=float(form['lon']) if form.get('lon') else None
        if (lat is None)!=(lon is None): raise ValueError()
        if lat is not None and (not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180): raise ValueError()
        address=str(form.get('delivery_address','')).strip()[:500]
        if lat is None and address:
            try:
                point=await geocode_address(address)
                if point: lat,lon=point
            except Exception:
                log.exception('Configured address lookup failed for a customer delivery address')
        if not address and lat is None: raise ValueError()
        if not address: address='GPS location'
    except Exception: return RedirectResponse('/ui/checkout?notice=Add+an+address+or+allow+location',303)
    lines=[]
    cart=session_cart(request)
    if not cart: raise HTTPException(422,'Your bag is empty')
    async with get_pool(request).acquire() as conn:
        products=await conn.fetch("SELECT id,default_unit FROM products WHERE status='active' AND id=ANY($1::uuid[])",[UUID(k) for k in cart])
    if len(products)!=len(cart): raise HTTPException(409,'An item in your bag is no longer available; remove it and try again')
    for product in products:
        try: qty=Decimal(cart.get(str(product["id"]),'0'))
        except InvalidOperation: raise HTTPException(422,'Enter valid product quantities')
        if not qty.is_finite() or qty<=0 or qty.as_tuple().exponent < -3 or qty.adjusted() >= 9:
            raise HTTPException(422,'Quantity must fit numeric(12,3) and be non-negative')
        lines.append((product['id'],qty,product['default_unit'],bool(form.get(f'substitute_{product["id"]}'))))
    if not lines: raise HTTPException(422,'Choose at least one product quantity')
    order_id=uuid4(); key='ui-'+secrets.token_urlsafe(18)
    hash_payload=json.dumps({"lines":[[str(x[0]),str(x[1]),x[3]] for x in lines],"address":address,"lat":lat,"lon":lon},sort_keys=True,separators=(',',':'))
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            cart={str(product):str(qty) for product,qty,_,_ in lines}
            quote=await quote_basket(conn,cart,lat,lon,lock=True)
            has_commitments=any(plan['selected'] for plan in quote['plans'])
            initial_status='sourcing' if has_commitments else 'needs_review'
            await conn.execute("INSERT INTO orders(id,customer_id,status,delivery_point,delivery_address,delivery_window,estimated_total_paise,estimated_delivery_minutes,estimated_route_distance_m,idempotency_key,request_hash) VALUES($1,$2,$3,CASE WHEN $4::double precision IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint($5,$4),4326)::geography END,$6,NULL,$7,$8,$9,$10,$11)",order_id,user['id'],initial_status,lat,lon,address,quote['total_paise'],quote['eta_minutes'],quote['distance_metres'],key,hashlib.sha256(hash_payload.encode()).hexdigest())
            plan_by_product={plan['product_id']:plan for plan in quote['plans']}
            for product,qty,unit,substitute in lines:
                line_id=await conn.fetchval("INSERT INTO order_lines(order_id,product_id,quantity,unit,allow_substitution) VALUES($1,$2,$3,$4,$5) RETURNING id",order_id,product,qty,unit,substitute)
                plan=plan_by_product[product]
                for picked in plan['selected']:
                    offer=picked['offer']; take=picked['quantity']; commitment_id=uuid4()
                    changed=await conn.execute("UPDATE produce_offers SET available_quantity=available_quantity-$2,status=CASE WHEN available_quantity-$2<=0 THEN 'committed' ELSE status END WHERE id=$1 AND available_quantity >= $2",offer['offer_id'],take)
                    if changed!='UPDATE 1': raise HTTPException(409,'Produce availability changed; please review your bag and try again')
                    ckey=f'order:{order_id}:offer:{offer["offer_id"]}:line:{line_id}'
                    digest=hashlib.sha256(f'{order_id}:{line_id}:{offer["offer_id"]}:{take}'.encode()).hexdigest()
                    await conn.execute("""INSERT INTO vendor_commitments(id,farm_id,product_id,delivery_date,requested_quantity,unit,ready_by,status,idempotency_key,request_hash,order_id,order_line_id,offer_id)
                        VALUES($1,$2,$3,CURRENT_DATE,$4,$5,now()+interval '6 hours','requested',$6,$7,$8,$9,$10)""",
                        commitment_id,offer['farm_id'],product,take,unit,ckey,digest,order_id,line_id,offer['offer_id'])
                    await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('vendor_commitment',$1,'vendor.commitment_requested',$2::jsonb)",commitment_id,json.dumps({'commitment_id':str(commitment_id),'order_id':str(order_id),'product_id':str(product)}))
            if initial_status=='sourcing':
                await conn.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'received','sourcing','Automatic vendor requests sent using available offers')",order_id)
            else:
                await conn.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'received','needs_review','No vendor offers are currently available')",order_id)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.received',$2::jsonb)",order_id,json.dumps({'order_id':str(order_id),'status':initial_status}))
    request.session['cart']={}
    return RedirectResponse('/ui/dashboard?notice=Order+placed',303)


@router.post("/ui/customer/cancel")
async def customer_cancel(request: Request):
    form=await request.form(); user=await form_user(request,form,{"customer"})
    try: oid=UUID(str(form['id']))
    except Exception: raise HTTPException(422,'Invalid order ID')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            old=await conn.fetchval("SELECT status FROM orders WHERE id=$1 AND customer_id=$2 FOR UPDATE",oid,user['id'])
            if old is None: raise HTTPException(404,'Order not found')
            if old not in {'received','sourcing','confirmed'}: raise HTTPException(409,'This order can no longer be cancelled from the customer portal')
            await conn.execute("UPDATE orders SET status='cancelled',updated_at=now() WHERE id=$1",oid)
            await release_order_offers(conn,oid)
            await conn.execute("UPDATE vendor_commitments SET status='cancelled',updated_at=now() WHERE order_id=$1 AND status IN ('requested','confirmed')",oid)
            await conn.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,$2,'cancelled','Customer requested cancellation')",oid,old)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.cancelled',$2::jsonb)",oid,'{"order_id":"'+str(oid)+'"}')
    return RedirectResponse('/ui/dashboard?notice=Order+cancellation+recorded',303)


@router.post("/ui/farmer/commitment")
async def farmer_commitment(request: Request):
    form=await request.form(); user=await form_user(request,form,{"farmer","vendor"})
    try:
        cid=UUID(str(form['id'])); state=str(form['status']); quantity=Decimal(str(form['quantity'])) if form.get('quantity') else None
        ready=datetime.fromisoformat(str(form['ready_at'])) if form.get('ready_at') else None
        if ready and ready.tzinfo is None: ready=ready.replace(tzinfo=timezone(timedelta(hours=5,minutes=30)))
        if state not in {'confirmed','shortfall','rejected'}: raise ValueError()
        if state in {'confirmed','shortfall'} and (quantity is None or not valid_quantity(quantity,True) or ready is None): raise ValueError()
    except Exception: raise HTTPException(422,'Check commitment response fields')
    async with get_pool(request).acquire() as conn:
        if not user['farm_id']: raise HTTPException(403,'Account is not linked to a farm')
        async with conn.transaction():
            row=await conn.fetchrow("UPDATE vendor_commitments SET status=$3,confirmed_quantity=$4,confirmed_ready_at=$5,response_note='Farmer portal response',updated_at=now() WHERE id=$1 AND farm_id=$2 AND status='requested' RETURNING id,order_id,offer_id,requested_quantity",cid,user['farm_id'],state,quantity,ready)
            if not row: raise HTTPException(404,'Request not found or already answered')
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('vendor_commitment',$1,'vendor.commitment_responded',$2::jsonb)",cid,'{"commitment_id":"'+str(cid)+'","status":"'+state+'"}')
            if row['offer_id']:
                release=Decimal(row['requested_quantity'])-(quantity or Decimal(0))
                if release>0:
                    await conn.execute("UPDATE produce_offers SET available_quantity=available_quantity+$2,status='available' WHERE id=$1",row['offer_id'],release)
            if row['order_id']:
                await queue_order_route(conn,row['order_id'])
    return RedirectResponse('/ui/dashboard?notice=Commitment+response+saved',303)


@router.post("/ui/farmer/photo")
async def farmer_photo(request: Request, file: UploadFile=File(...), caption: str=Form(default=""), csrf: str=Form(...)):
    form={"csrf":csrf}; user=await form_user(request,form,{"farmer","vendor"})
    if not user['farm_id']: raise HTTPException(403,'Account is not linked to a farm')
    if file.content_type not in {'image/jpeg','image/png','image/webp'}: raise HTTPException(415,'Upload JPEG, PNG, or WebP')
    data=await file.read(8*1024*1024+1)
    if not data or len(data)>8*1024*1024: raise HTTPException(413,'Photo limit is 8 MiB')
    if not is_image(file.content_type,data): raise HTTPException(415,'File contents do not match the selected image type')
    async with get_pool(request).acquire() as conn:
        await conn.execute("INSERT INTO farm_photos(farm_id,content_type,caption,data) VALUES($1,$2,$3,$4)",user['farm_id'],file.content_type,caption[:240],data)
    return RedirectResponse('/ui/dashboard?notice=Photo+uploaded',303)


@router.post("/ui/farmer/offer")
async def farmer_offer(request: Request):
    form=await request.form(); user=await form_user(request,form,{"farmer","vendor"})
    if not user['farm_id']: raise HTTPException(403,'Account is not linked to a farm')
    try:
        product=UUID(str(form['product_id'])); quantity=Decimal(str(form['quantity'])); harvest=datetime.fromisoformat(str(form['harvest_date'])).date()
        price=Decimal(str(form['price']))
        ready=datetime.fromisoformat(str(form['ready_at'])) if form.get('ready_at') else None
        if not valid_quantity(quantity) or not price.is_finite() or price<=0 or price>Decimal('100000') or harvest<datetime.now().date(): raise ValueError()
        if ready and ready.tzinfo is None: ready=ready.replace(tzinfo=timezone(timedelta(hours=5,minutes=30)))
    except Exception: raise HTTPException(422,'Check product, quantity, date, and readiness fields')
    async with get_pool(request).acquire() as conn:
        product_row=await conn.fetchrow("SELECT default_unit FROM products WHERE id=$1 AND status='active'",product)
        if not product_row: raise HTTPException(422,'Choose an active product')
        offer_id=uuid4()
        async with conn.transaction():
            await conn.execute("INSERT INTO produce_offers(id,farm_id,product_id,available_quantity,unit,unit_price_paise,harvest_date,ready_at,status) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'available')",offer_id,user['farm_id'],product,quantity,product_row['default_unit'],int(price*100),harvest,ready)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('produce_offer',$1,'produce.offer_created',$2::jsonb)",offer_id,'{"offer_id":"'+str(offer_id)+'"}')
    return RedirectResponse('/ui/dashboard?notice=Produce+offer+published',303)


@router.post("/ui/farmer/product")
async def farmer_product_create(request: Request):
    form=await request.form(); user=await form_user(request,form,{"farmer","vendor"})
    if not user['farm_id']: raise HTTPException(403,'Account is not linked to a farm')
    name=str(form.get('name','')).strip()[:120]
    category=str(form.get('category','')).strip()[:80]
    unit=str(form.get('unit','kg')).strip()[:24]
    try:
        quantity=Decimal(str(form.get('quantity',''))); price=Decimal(str(form.get('price','')))
        harvest=datetime.fromisoformat(str(form.get('harvest_date',''))).date()
        if not valid_quantity(quantity) or not price.is_finite() or price<=0 or price>Decimal('100000') or harvest<datetime.now().date(): raise ValueError()
    except Exception: raise HTTPException(422,'Enter a product name, quantity, positive price, and valid harvest date')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            product_id=await conn.fetchval("INSERT INTO products(name,category,default_unit,max_transit_minutes) VALUES($1,$2,$3,720) ON CONFLICT(name) DO UPDATE SET status='active' RETURNING id",name,category,unit)
            offer_id=uuid4()
            await conn.execute("INSERT INTO produce_offers(id,farm_id,product_id,available_quantity,unit,unit_price_paise,harvest_date,status) VALUES($1,$2,$3,$4,$5,$6,$7,'available')",offer_id,user['farm_id'],product_id,quantity,unit,int(price*100),harvest)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('produce_offer',$1,'produce.offer_created',$2::jsonb)",offer_id,json.dumps({'offer_id':str(offer_id),'product_id':str(product_id)}))
    return RedirectResponse('/ui/dashboard?notice=Product+and+offer+published',303)


@router.post("/ui/farmer/withdraw")
async def farmer_withdraw_offer(request: Request):
    form=await request.form(); user=await form_user(request,form,{"farmer","vendor"})
    if not user['farm_id']: raise HTTPException(403,'Account is not linked to a farm')
    try: offer=UUID(str(form['id']))
    except Exception: raise HTTPException(422,'Invalid offer ID')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            row=await conn.fetchrow("UPDATE produce_offers SET status='unavailable' WHERE id=$1 AND farm_id=$2 AND status='available' RETURNING id",offer,user['farm_id'])
            if not row: raise HTTPException(404,'Available offer not found for your farm')
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('produce_offer',$1,'produce.offer_withdrawn',$2::jsonb)",offer,'{"offer_id":"'+str(offer)+'"}')
    return RedirectResponse('/ui/dashboard?notice=Offer+withdrawn',303)


@router.post("/ui/driver/response")
async def driver_response(request: Request):
    form=await request.form(); user=await form_user(request,form,{"driver"})
    try: aid=UUID(str(form['id'])); state=str(form['status'])
    except Exception: raise HTTPException(422,'Invalid assignment ID')
    if state not in {'accepted','declined'}: raise HTTPException(422,'Invalid response')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            row=await conn.fetchrow("""UPDATE driver_task_assignments a SET status=$3 FROM route_stops anchor
                WHERE a.id=$1 AND a.driver_id=$2 AND a.status='offered' AND anchor.task_id=a.task_id
                RETURNING anchor.planning_job_id,anchor.vehicle_id""",aid,user['id'],state)
            if not row: raise HTTPException(404,'Assignment not found or already answered')
            await conn.execute("UPDATE route_stops SET status=$3,updated_at=now() WHERE planning_job_id=$1 AND vehicle_id=$2 AND status='offered'",row['planning_job_id'],row['vehicle_id'],'assigned' if state=='accepted' else 'planned')
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('driver_assignment',$1,$2,$3::jsonb)",aid,'driver.assignment_'+state,'{"assignment_id":"'+str(aid)+'","status":"'+state+'"}')
            if state=='declined':
                await offer_route_to_next_driver(conn,row['planning_job_id'],row['vehicle_id'])
    return RedirectResponse('/ui/dashboard?notice=Assignment+response+saved',303)


@router.post("/ui/driver/event")
async def driver_event(request: Request):
    form=await request.form(); user=await form_user(request,form,{"driver"})
    try: sid=UUID(str(form['id'])); event=str(form['event'])
    except Exception: raise HTTPException(422,'Invalid stop/event')
    if event not in {'arrived','picked_up','delivered','failed','quality_rejected'}: raise HTTPException(422,'Invalid event')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            row=await conn.fetchrow("""SELECT s.id,s.order_id,s.planning_job_id,s.vehicle_id FROM route_stops s
                JOIN driver_task_assignments a ON a.driver_id=$2 AND a.status='accepted'
                JOIN route_stops anchor ON anchor.task_id=a.task_id AND anchor.planning_job_id=s.planning_job_id AND anchor.vehicle_id=s.vehicle_id
                WHERE s.id=$1""",sid,user['id'])
            if not row: raise HTTPException(404,'Assigned stop not found')
            key='ui-'+secrets.token_urlsafe(18); eid=uuid4()
            await conn.execute("INSERT INTO delivery_events(id,route_stop_id,idempotency_key,request_hash,event_type,occurred_at,note) VALUES($1,$2,$3,$4,$5,now(),$6)",eid,sid,key,hashlib.sha256(f'{sid}:{event}:{form.get("note","")}'.encode()).hexdigest(),event,str(form.get('note',''))[:500])
            await conn.execute("UPDATE route_stops SET status=$2,updated_at=now() WHERE id=$1",sid,event)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('route_stop',$1,'delivery.event_recorded',$2::jsonb)",sid,'{"event_id":"'+str(eid)+'","event_type":"'+event+'"}')
            if event=='picked_up' and row['order_id']:
                old=await conn.fetchval("UPDATE orders SET status='dispatched',updated_at=now() WHERE id=$1 AND status='planning' RETURNING status",row['order_id'])
                if old:
                    await conn.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'planning','dispatched','Driver confirmed pickup')",row['order_id'])
                    await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.dispatched',$2::jsonb)",row['order_id'],json.dumps({'order_id':str(row['order_id'])}))
            if event=='delivered' and row['order_id']:
                remains=await conn.fetchval("SELECT count(*) FROM route_stops WHERE order_id=$1 AND stop_type='delivery' AND status<>'delivered'",row['order_id'])
                if remains==0:
                    old=await conn.fetchval("UPDATE orders SET status='delivered',updated_at=now() WHERE id=$1 AND status='dispatched' RETURNING status",row['order_id'])
                    if old:
                        await conn.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'dispatched','delivered','All delivery stops completed')",row['order_id'])
                        await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.delivered',$2::jsonb)",row['order_id'],'{"order_id":"'+str(row['order_id'])+'"}')
    return RedirectResponse('/ui/dashboard?notice=Stop+event+recorded',303)


@router.post("/ui/operations/simulate")
async def operations_simulate(request: Request):
    form=await request.form(); user=await form_user(request,form,{"operations"})
    try: farms=max(1,min(100,int(form['farms']))); offers=max(1,min(20,int(form['offers']))); seed=int(form['seed'])
    except Exception: raise HTTPException(422,'Invalid simulation values')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            product_count=await conn.fetchval("SELECT count(*) FROM products WHERE status='active'")
            if not product_count: raise HTTPException(409,'Add active products before generating demo data')
            frows=await conn.fetch("INSERT INTO farms(name,location) SELECT 'Demo Farm '||$1||'-'||n,ST_SetSRID(ST_MakePoint(77.0+((n*7919+$2)%1000)/10000.0,28.0+((n*3571+$2)%1000)/10000.0),4326)::geography FROM generate_series(1,$3) n RETURNING id",str(seed),seed,farms)
            ids=[r['id'] for r in frows]
            await conn.execute("INSERT INTO produce_offers(farm_id,product_id,available_quantity,unit,unit_price_paise,harvest_date,quality_grade) SELECT f.id,p.id,20+((g*37+$1)%181),p.default_unit,5000+((g*137+$1)%15000),CURRENT_DATE+((g*13+$1)%7)::int,'A' FROM unnest($2::uuid[]) WITH ORDINALITY f(id,fi) CROSS JOIN generate_series(1,$3) g CROSS JOIN LATERAL(SELECT id,default_unit FROM products WHERE status='active' ORDER BY id OFFSET ((f.fi+g+$1)%$4) LIMIT 1)p",seed,ids,offers,product_count)
    return RedirectResponse('/ui/dashboard?notice=Demo+farms+and+offers+created',303)


@router.post("/ui/operations/planning")
async def operations_planning(request: Request):
    form=await request.form(); user=await form_user(request,form,{"operations"})
    try:
        raw=json.loads(str(form['payload']))
        payload=PlanningJobCreate.model_validate(raw)
    except Exception as exc:
        raise HTTPException(422,f'Invalid planning payload: {str(exc)[:500]}')
    job_id=uuid4(); key='ui-'+secrets.token_urlsafe(18)
    serialized=json.dumps(payload.model_dump(mode='json'),sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(serialized.encode()).hexdigest()
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO planning_jobs(id,status,idempotency_key,request_hash,payload) VALUES($1,'queued',$2,$3,$4::jsonb)",job_id,key,digest,serialized)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('planning_job',$1,'planning.requested',$2::jsonb)",job_id,'{"planning_job_id":"'+str(job_id)+'"}')
    return RedirectResponse('/ui/dashboard?notice=Planning+job+queued',303)


@router.post("/ui/operations/api-call", response_class=HTMLResponse)
async def operations_api_call(request: Request):
    form=await request.form(); await form_user(request,form,{"operations"})
    kind=str(form.get('kind',''))
    paths={'pay-quotes':'/v1/operations/pay-quotes','driver-allocations':'/v1/operations/driver-allocations'}
    if kind not in paths: raise HTTPException(422,'Unsupported operations action')
    try: payload=json.loads(str(form['payload']))
    except json.JSONDecodeError: raise HTTPException(422,'Request must be valid JSON')
    api_key=os.environ.get('API_KEY')
    if not api_key: raise HTTPException(503,'API_KEY is not configured')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app),base_url='http://internal',timeout=15) as client:
        response=await client.post(paths[kind],json=payload,headers={'X-API-Key':api_key,'Idempotency-Key':'ui-'+secrets.token_urlsafe(18)})
    try: result=json.dumps(response.json(),indent=2,default=str)
    except Exception: result=response.text
    return page('Operations result',f'<h1>{esc(kind.replace("-"," ").title())} result</h1><section><p>HTTP {response.status_code}</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(result)}</pre><a href="/ui/dashboard">Back to operations</a></section>',await current_user(request))


@router.post("/ui/operations/order-status")
async def operations_order_status(request: Request):
    form=await request.form(); user=await form_user(request,form,{"operations"})
    try: oid=UUID(str(form['id'])); target=str(form['status'])
    except Exception: raise HTTPException(422,'Invalid order/status')
    allowed={'received':{'sourcing','cancelled','needs_review'},'sourcing':{'confirmed','cancelled','needs_review'},'confirmed':{'planning','cancelled','needs_review'},'planning':{'dispatched','cancelled','needs_review'},'dispatched':{'delivered','needs_review'},'needs_review':{'sourcing','confirmed','planning','cancelled'}}
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            old=await conn.fetchval("SELECT status FROM orders WHERE id=$1 FOR UPDATE",oid)
            if old is None: raise HTTPException(404,'Order not found')
            if target not in allowed.get(old,set()): raise HTTPException(409,f'Invalid order transition {old} -> {target}')
            await conn.execute("UPDATE orders SET status=$2,updated_at=now() WHERE id=$1",oid,target)
            await conn.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,$2,$3,$4)",oid,old,target,str(form.get('note',''))[:500])
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,$2,$3::jsonb)",oid,'order.'+target,'{"order_id":"'+str(oid)+'"}')
    return RedirectResponse('/ui/dashboard?notice=Order+status+updated',303)


@router.post("/ui/operations/order-location")
async def operations_order_location(request: Request):
    form=await request.form(); await form_user(request,form,{"operations"})
    try:
        oid=UUID(str(form['id'])); address=str(form.get('address','')).strip()[:500]
        if not address: raise ValueError()
    except Exception: raise HTTPException(422,'Enter a valid order ID and delivery address')
    point=await geocode_address(address)
    if not point: raise HTTPException(422,'Address lookup did not find a delivery point. Configure GEOCODER_URL or ask the customer to share their current location.')
    lat,lon=point
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            changed=await conn.fetchval("UPDATE orders SET delivery_address=$2,delivery_point=ST_SetSRID(ST_MakePoint($3,$4),4326)::geography,updated_at=now() WHERE id=$1 RETURNING id",oid,address,lon,lat)
            if not changed: raise HTTPException(404,'Order not found')
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.delivery_location_added',$2::jsonb)",oid,json.dumps({"order_id":str(oid),"latitude":lat,"longitude":lon}))
            await queue_order_route(conn,oid)
    return RedirectResponse('/ui/dashboard?notice=Delivery+pin+saved',303)


@router.post("/ui/operations/quality")
async def operations_quality(request: Request):
    form=await request.form(); user=await form_user(request,form,{"operations"})
    try: offer=UUID(str(form['id'])); passed=str(form['passed'])=='true'; grade=str(form['grade'])[:40]; inspector=str(form['inspector'])[:120]
    except Exception: raise HTTPException(422,'Invalid check')
    async with get_pool(request).acquire() as conn:
        async with conn.transaction():
            exists=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM produce_offers WHERE id=$1)",offer)
            if not exists: raise HTTPException(404,'Offer not found')
            await conn.execute("INSERT INTO quality_checks(offer_id,grade,passed,inspector,notes) VALUES($1,$2,$3,$4,$5)",offer,grade,passed,inspector,str(form.get('notes',''))[:1000])
            if not passed: await conn.execute("UPDATE produce_offers SET status='unavailable' WHERE id=$1",offer)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('produce_offer',$1,'produce.quality_checked',$2::jsonb)",offer,'{"offer_id":"'+str(offer)+'","passed":'+str(passed).lower()+'}')
    return RedirectResponse('/ui/dashboard?notice=Quality+check+saved',303)


@router.post("/ui/operations/commitment")
async def operations_commitment(request: Request):
    form=await request.form(); user=await form_user(request,form,{"operations"})
    try:
        farm=UUID(str(form['farm_id'])); product=UUID(str(form['product_id'])); qty=Decimal(str(form['quantity'])); day=datetime.fromisoformat(str(form['date'])).date(); ready=datetime.fromisoformat(str(form['ready_by']));
        if ready.tzinfo is None: ready=ready.replace(tzinfo=timezone(timedelta(hours=5,minutes=30)))
        if not valid_quantity(qty): raise ValueError()
    except Exception: raise HTTPException(422,'Check commitment fields')
    async with get_pool(request).acquire() as conn:
        cid=uuid4(); key='ui-'+secrets.token_urlsafe(18); digest=hashlib.sha256(f'{farm}:{product}:{day}:{qty}'.encode()).hexdigest()
        async with conn.transaction():
            await conn.execute("INSERT INTO vendor_commitments(id,farm_id,product_id,delivery_date,requested_quantity,unit,ready_by,status,idempotency_key,request_hash) VALUES($1,$2,$3,$4,$5,$6,$7,'requested',$8,$9)",cid,farm,product,day,qty,str(form['unit'])[:24],ready,key,digest)
            await conn.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('vendor_commitment',$1,'vendor.commitment_requested',$2::jsonb)",cid,'{"commitment_id":"'+str(cid)+'"}')
    return RedirectResponse('/ui/dashboard?notice=Commitment+request+created',303)


@router.post("/ui/operations/users")
async def operations_user_create(request: Request):
    form=await request.form(); await form_user(request,form,{"operations"})
    name=str(form.get('full_name','')).strip()[:120]; email=str(form.get('email','')).strip().lower(); phone=str(form.get('phone','')).strip()
    role=str(form.get('role',''))
    if not name or '@' not in email or not re.fullmatch(r"\+[1-9][0-9]{7,14}",phone) or role not in {'farmer','vendor','driver','operations'}:
        raise HTTPException(422,'Enter a name, valid email, international phone, and role')
    farm_id=UUID(str(form['farm_id'])) if form.get('farm_id') else None
    if role in {'farmer','vendor'} and not farm_id: raise HTTPException(422,'Farmer and vendor accounts must be linked to a farm')
    if role not in {'farmer','vendor'}: farm_id=None
    async with get_pool(request).acquire() as conn:
        try:
            new_user=await conn.fetchrow("INSERT INTO platform_users(full_name,email,phone,role,farm_id) VALUES($1,$2,$3,$4,$5) RETURNING id",name,email,phone,role,farm_id)
        except asyncpg.UniqueViolationError:
            raise HTTPException(409,'That email or phone is already registered')
        except asyncpg.CheckViolationError as exc:
            raise HTTPException(409,'The database role constraint is out of date. Apply migrations/007_vendor_role_constraint.sql, then retry.') from exc
        if role=='driver':
            ready=await conn.fetch("SELECT id FROM orders WHERE status='confirmed' AND planning_job_id IS NULL AND delivery_point IS NOT NULL ORDER BY created_at LIMIT 100")
            for item in ready:
                try: await queue_order_route(conn,item['id'])
                except Exception: log.exception('Could not queue confirmed order %s after driver onboarding',item['id'])
    return RedirectResponse('/ui/dashboard?notice=Role+account+created',303)


@router.post("/ui/operations/users/{account_id}/farm")
async def operations_user_farm_update(request: Request, account_id: UUID):
    form=await request.form(); await form_user(request,form,{"operations"})
    try: farm_id=UUID(str(form.get('farm_id','')))
    except (TypeError,ValueError): raise HTTPException(422,'Choose a valid farm')
    async with get_pool(request).acquire() as conn:
        exists=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM farms WHERE id=$1 AND status='active')",farm_id)
        if not exists: raise HTTPException(422,'Choose an active farm')
        result=await conn.execute("UPDATE platform_users SET farm_id=$2 WHERE id=$1 AND role IN ('farmer','vendor')",account_id,farm_id)
        if result=='UPDATE 0': raise HTTPException(404,'Farmer or vendor account not found')
    return RedirectResponse('/ui/dashboard?notice=Farm+link+updated',303)


@router.post("/ui/operations/farm")
async def operations_farm_create(request: Request):
    form=await request.form(); await form_user(request,form,{"operations"})
    name=str(form.get('name','')).strip()[:160]
    address=str(form.get('address','')).strip()[:500]
    try:
        lat=float(form.get('lat','')) if form.get('lat') else None
        lon=float(form.get('lon','')) if form.get('lon') else None
        if (lat is None)!=(lon is None): raise ValueError()
    except (TypeError,ValueError): raise HTTPException(422,'Invalid farm location')
    if lat is None and address:
        try: point=await geocode_address(address)
        except Exception as exc: raise HTTPException(422,'Farm address lookup failed; check GEOCODER_URL or use the device location') from exc
        if point: lat,lon=point
    if not name or not address or lat is None or lon is None or not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
        raise HTTPException(422,'Enter the farm address and either allow this device location or configure GEOCODER_URL to look up addresses')
    async with get_pool(request).acquire() as conn:
        await conn.execute("INSERT INTO farms(name,address,location) VALUES($1,$2,ST_SetSRID(ST_MakePoint($3,$4),4326)::geography)",name,address,lon,lat)
    return RedirectResponse('/ui/dashboard?notice=Farm+created',303)


@router.post("/ui/operations/product")
async def operations_product_create(request: Request):
    form=await request.form(); await form_user(request,form,{"operations"})
    name=str(form.get('name','')).strip()[:120]
    category=str(form.get('category','')).strip()[:80]
    unit=str(form.get('unit','')).strip()[:24]
    try: transit=int(form.get('transit','240'))
    except (TypeError,ValueError): raise HTTPException(422,'Enter a valid maximum transit time')
    if not name or not category or not unit or transit<1 or transit>10080:
        raise HTTPException(422,'Enter product details and a transit time between 1 and 10080 minutes')
    async with get_pool(request).acquire() as conn:
        try: await conn.execute("INSERT INTO products(name,category,default_unit,max_transit_minutes) VALUES($1,$2,$3,$4)",name,category,unit,transit)
        except asyncpg.UniqueViolationError: raise HTTPException(409,'A product with that name already exists')
    return RedirectResponse('/ui/dashboard?notice=Product+created',303)


@router.post("/ui/logout")
async def logout(request: Request):
    form=await request.form(); check_csrf(request,form); request.session.clear()
    return RedirectResponse('/ui/login',303)
