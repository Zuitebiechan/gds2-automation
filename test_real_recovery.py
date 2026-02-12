"""
Day 7 真实测试脚本

测试AI恢复系统与真实GDS2的集成。
"""

import logging
from pathlib import Path
import json

# Load .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ 已加载环境变量: {env_path}\n")
except ImportError:
    print("⚠️  python-dotenv未安装，请运行: pip install python-dotenv\n")

from src.workflows.data_viewer import DataViewerWorkflow

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_normal_workflow():
    """测试1：正常流程（无异常）"""
    print("\n" + "="*70)
    print("测试1：正常流程（无异常）")
    print("="*70)

    workflow = DataViewerWorkflow(enable_ai_recovery=True)

    if not workflow.recovery:
        print("❌ AI recovery未启用，检查配置")
        return False

    print(f"✅ AI recovery已启用")
    print(f"   Provider: {workflow.recovery.config.provider}")
    print(f"   Model: {workflow.recovery.config.model}")

    try:
        # 启动workflow
        print("\n1. 启动workflow...")
        result = workflow.start()

        if "devices" in result:
            print(f"✅ 检测到设备列表：{result['devices']}")
            print("\n请选择一个设备并调用 workflow.connect_device()")
        elif "modules" in result:
            print(f"✅ 已连接设备，检测到 {len(result['modules'])} 个模块")
            print(f"   VIN: {result.get('vin', 'N/A')}")

        # 检查AI调用次数
        if workflow.recovery:
            print(f"\n📊 LLM调用次数: {workflow.recovery.llm_call_count}")

        return True

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_with_simulated_timeout():
    """测试2：模拟超时场景（需要手动触发）"""
    print("\n" + "="*70)
    print("测试2：超时场景")
    print("="*70)
    print("\n⚠️  手动测试步骤：")
    print("   1. 拔掉VCI设备")
    print("   2. 调用 workflow.connect_device('SM2 USB')")
    print("   3. 观察AI是否检测到超时并恢复")
    print("   4. 检查 logs/ai_recovery/decisions.jsonl")


def test_with_simulated_dialog():
    """测试3：模拟弹窗场景（需要手动触发）"""
    print("\n" + "="*70)
    print("测试3：弹窗场景")
    print("="*70)
    print("\n⚠️  手动测试步骤：")
    print("   1. 在GDS2操作过程中触发错误对话框")
    print("   2. 观察AI是否检测到弹窗")
    print("   3. AI应自动点击OK/确定按钮")
    print("   4. 检查 logs/ai_recovery/decisions.jsonl")


def check_decision_logs():
    """检查AI决策日志"""
    print("\n" + "="*70)
    print("检查AI决策日志")
    print("="*70)

    log_file = Path("logs/ai_recovery/decisions.jsonl")

    if not log_file.exists():
        print("ℹ️  还没有决策日志")
        return

    print(f"\n📁 日志文件: {log_file}")

    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    print(f"📊 总决策数: {len(lines)}")

    if lines:
        print("\n最近5条决策：")
        for line in lines[-5:]:
            try:
                entry = json.loads(line)
                print(f"\n  时间: {entry['timestamp']}")
                print(f"  异常: {entry['anomaly']['type']}")
                print(f"  决策: {entry['decision']['action']}")
                print(f"  置信度: {entry['decision']['confidence']:.2f}")
                print(f"  原因: {entry['decision']['reasoning']}")
            except:
                pass


if __name__ == "__main__":
    print("\n🎯 Day 7 - 真实测试")
    print("AI异常恢复系统 + 真实GDS2 + 智谱AI GLM-4-Plus")
    print()

    # 测试1：正常流程
    success = test_normal_workflow()

    if success:
        print("\n✅ 基础功能测试通过！")

        # 显示手动测试说明
        test_with_simulated_timeout()
        test_with_simulated_dialog()

        # 检查决策日志
        check_decision_logs()

        print("\n" + "="*70)
        print("📝 测试清单")
        print("="*70)
        print("  [ ] 正常流程（无异常）")
        print("  [ ] 超时恢复（拔VCI设备）")
        print("  [ ] 弹窗恢复（触发错误对话框）")
        print("  [ ] 检查决策日志")
        print("  [ ] 验证成本（检查API调用次数）")
        print()
