"""
配置验证脚本

在运行Day 7测试前，验证AI恢复系统配置是否正确。
"""

import os
import sys
from pathlib import Path

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ 已加载环境变量: {env_path}")
    else:
        print(f"⚠️  .env文件不存在: {env_path}")
except ImportError:
    print("⚠️  python-dotenv未安装，无法自动加载.env文件")
    print("   运行: pip install python-dotenv")
    print("   或手动设置环境变量")
    print()


def check_env_vars():
    """检查环境变量配置"""
    print("\n" + "="*70)
    print("1. 检查环境变量配置")
    print("="*70)

    required_vars = {
        "ENABLE_AI_RECOVERY": "true",
        "AI_RECOVERY_PROVIDER": "zhipuai",
        "AI_RECOVERY_MODEL": "glm-4-plus",
        "ZHIPUAI_API_KEY": None,  # Just check it exists
    }

    optional_vars = {
        "AI_RECOVERY_CONFIDENCE": "0.7",
        "AI_RECOVERY_MAX_RETRIES": "2",
        "AI_RECOVERY_MAX_CALLS": "3",
        "AI_RECOVERY_LOG_DECISIONS": "true",
        "AI_RECOVERY_LOG_DIR": "logs/ai_recovery",
    }

    all_ok = True

    # Check required
    for var, expected in required_vars.items():
        value = os.getenv(var)
        if value is None:
            print(f"  ❌ {var}: 未设置")
            all_ok = False
        elif expected and value != expected:
            print(f"  ⚠️  {var}: {value} (预期: {expected})")
        else:
            # Mask API key
            display_value = value[:10] + "..." if var.endswith("API_KEY") else value
            print(f"  ✅ {var}: {display_value}")

    # Check optional
    print("\n  可选配置:")
    for var, default in optional_vars.items():
        value = os.getenv(var)
        if value:
            print(f"  ✅ {var}: {value}")
        else:
            print(f"  ℹ️  {var}: 使用默认值 ({default})")

    return all_ok


def check_config_loading():
    """测试配置加载"""
    print("\n" + "="*70)
    print("2. 测试配置加载")
    print("="*70)

    try:
        from src.recovery import AIRecoveryConfig

        config = AIRecoveryConfig.from_env()

        print(f"  ✅ Provider: {config.provider}")
        print(f"  ✅ Model: {config.model}")
        print(f"  ✅ Enabled: {config.enabled}")
        print(f"  ✅ API Key: {config.api_key[:10]}..." if config.api_key else "  ❌ API Key: 未设置")
        print(f"  ✅ Confidence: {config.confidence_threshold}")
        print(f"  ✅ Max Retries: {config.max_retries}")
        print(f"  ✅ Max LLM Calls: {config.max_llm_calls_per_session}")

        # Validate
        errors = config.validate()
        if not errors:
            print("\n  ✅ 配置验证通过!")
            return True
        else:
            print(f"\n  ❌ 配置错误:")
            for error in errors:
                print(f"     - {error}")
            return False

    except ImportError as e:
        print(f"  ❌ 导入失败: {e}")
        return False
    except Exception as e:
        print(f"  ❌ 加载失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_zhipuai_sdk():
    """检查智谱AI SDK"""
    print("\n" + "="*70)
    print("3. 检查智谱AI SDK")
    print("="*70)

    try:
        from zhipuai import ZhipuAI
        print("  ✅ zhipuai SDK已安装")

        # Try to create client (but don't call API)
        api_key = os.getenv("ZHIPUAI_API_KEY")
        if api_key:
            try:
                client = ZhipuAI(api_key=api_key)
                print("  ✅ 客户端创建成功")
                return True
            except Exception as e:
                print(f"  ❌ 客户端创建失败: {e}")
                return False
        else:
            print("  ⚠️  API Key未设置，无法验证客户端")
            return False

    except ImportError:
        print("  ❌ zhipuai SDK未安装")
        print("     运行: pip install zhipuai")
        return False


def check_log_directory():
    """检查日志目录"""
    print("\n" + "="*70)
    print("4. 检查日志目录")
    print("="*70)

    log_dir = Path(os.getenv("AI_RECOVERY_LOG_DIR", "logs/ai_recovery"))

    if log_dir.exists():
        print(f"  ✅ 日志目录存在: {log_dir}")

        # Check if writable
        try:
            test_file = log_dir / ".test"
            test_file.touch()
            test_file.unlink()
            print("  ✅ 日志目录可写")
            return True
        except Exception as e:
            print(f"  ❌ 日志目录不可写: {e}")
            return False
    else:
        print(f"  ℹ️  日志目录不存在: {log_dir}")
        print("     将在首次运行时自动创建")
        return True


def check_gds2_data_directory():
    """检查GDS2数据目录"""
    print("\n" + "="*70)
    print("5. 检查GDS2数据目录")
    print("="*70)

    gds2_data = Path.home() / "gds2-data"

    if gds2_data.exists():
        print(f"  ✅ GDS2数据目录存在: {gds2_data}")

        # Check for latest.json
        latest_json = gds2_data / "latest.json"
        if latest_json.exists():
            print(f"  ✅ latest.json存在 (GDS2 Java Agent运行中)")
            return True
        else:
            print(f"  ⚠️  latest.json不存在 (GDS2可能未启动)")
            return True  # Not a blocker
    else:
        print(f"  ⚠️  GDS2数据目录不存在: {gds2_data}")
        print("     请确保GDS2 + Java Agent已启动")
        return True  # Not a blocker for config check


def main():
    print("\n🔍 AI恢复系统配置验证")
    print("=" * 70)

    results = []

    results.append(("环境变量", check_env_vars()))
    results.append(("配置加载", check_config_loading()))
    results.append(("ZhipuAI SDK", check_zhipuai_sdk()))
    results.append(("日志目录", check_log_directory()))
    results.append(("GDS2数据目录", check_gds2_data_directory()))

    # Summary
    print("\n" + "="*70)
    print("📊 验证结果汇总")
    print("="*70)

    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"  {status} - {name}")

    all_passed = all(result for _, result in results)

    if all_passed:
        print("\n✅ 所有检查通过! 可以开始Day 7测试")
        print("\n运行测试:")
        print("  python test_real_recovery.py")
        return 0
    else:
        print("\n❌ 部分检查失败，请修复后重试")
        print("\n参考文档:")
        print("  docs/AI_RECOVERY_DAY7_GUIDE.md")
        return 1


if __name__ == "__main__":
    sys.exit(main())
