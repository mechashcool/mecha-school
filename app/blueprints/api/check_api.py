import requests

# 1. عنوان السيرفر (تأكد من الـ IP)
BASE_URL = "http://192.168.100.179:5000"

# 2. معلومات الحساب (لاحظ غيرنا email إلى username)
user_credentials = {
    "username": "aaa",  # غيرناها من email إلى username
    "password": "123456"
}

session = requests.Session()

print("--- محاولة تسجيل الدخول ---")
# نرسل البيانات للحصول على السشن
login_res = session.post(f"{BASE_URL}/auth/login", data=user_credentials)

# فحص إذا كان الرد هو تحويل (Redirect) أو نجاح
if login_res.status_code in [200, 302]: 
    print("✅ تم إرسال طلب الدخول!")
    
    # 3. نطلب بيانات الـ API هسة
    print("--- طلب بيانات الأبناء ---")
    api_res = session.get(f"{BASE_URL}/api/v1/parent/me")
    
    # فحص نوع الرد (لازم يكون JSON مو HTML)
    if "application/json" in api_res.headers.get("Content-Type", ""):
        print("🚀 هذي هي بيانات الـ JSON الحقيقية:")
        print(api_res.json())
    else:
        print("❌ السيرفر لسه يرجع صفحة HTML.. يبدو أن تسجيل الدخول لم ينجح فعلياً.")
        print("تأكد من صحة اسم المستخدم والباسورد، وتأكد أن الحساب هو 'Parent'.")
else:
    print(f"❌ فشل الاتصال بالسيرفر. الكود: {login_res.status_code}")