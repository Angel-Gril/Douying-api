"""
Cookie 获取辅助工具
帮助用户从浏览器获取抖音Cookie并写入配置
"""

from pathlib import Path


def get_cookie_instructions():
    print("""
╔══════════════════════════════════════════════════╗
║        抖音 Cookie 获取指南                      ║
╠══════════════════════════════════════════════════╣
║                                                  ║
║  1. 打开浏览器，访问 https://www.douyin.com      ║
║  2. 登录你的抖音账号                             ║
║  3. 按 F12 打开开发者工具                        ║
║  4. 切换到 Network (网络) 标签                   ║
║  5. 刷新页面                                     ║
║  6. 点击任意一个请求                             ║
║  7. 在 Request Headers 中找到 Cookie 字段        ║
║  8. 复制完整的 Cookie 值                         ║
║                                                  ║
║  关键Cookie字段:                                 ║
║  - msToken (必须)                                ║
║  - ttwid (必须)                                  ║
║  - odin_tt (推荐)                                ║
║  - sessionid (推荐，需登录)                      ║
║                                                  ║
╚══════════════════════════════════════════════════╝
""")


def save_cookie(cookie_str: str):
    """将 Cookie 保存到被 Git 忽略的 .env 文件中。"""
    if any(char in cookie_str for char in "\r\n"):
        print("\n[错误] Cookie 不能包含换行符")
        return

    try:
        env_path = Path(__file__).resolve().parent / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        lines = [line for line in lines if not line.startswith("DOUYIN_COOKIE=")]
        lines.append(f"DOUYIN_COOKIE={cookie_str}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        print("\n[成功] Cookie 已保存到 .env（该文件不会提交到 Git）")
        print("重启服务后生效: python server.py")

    except Exception as e:
        print(f"\n[错误] 保存失败: {e}")
        print("请手动设置环境变量 DOUYIN_COOKIE")


if __name__ == "__main__":
    get_cookie_instructions()
    print("\n请粘贴你的Cookie (一行，粘贴后按回车):")
    print("(输入 q 退出)\n")

    cookie = input("> ").strip()
    if cookie and cookie.lower() != 'q':
        save_cookie(cookie)
    else:
        print("已取消")
