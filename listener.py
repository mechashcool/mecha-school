from flask import Flask, request

app = Flask(__name__)

@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def catch_all(path):
    print("\n=== REQUEST RECEIVED ===")
    print("Path:", path)
    print("Method:", request.method)
    print("Headers:", dict(request.headers))
    raw = request.get_data()
    print("Raw Body:", raw)
    try:
        print("Body Text:", raw.decode("utf-8", errors="replace"))
    except Exception as e:
        print("Decode error:", e)
    return "OK"

if __name__ == "__main__":
    print("Listening on 0.0.0.0:7005 ...")
    app.run(host="0.0.0.0", port=7005)
