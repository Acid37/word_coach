"""word_coach 背单词助手插件包。

注意：不要在包初始化时 eager 导入 plugin.py——PluginManager 会直接按入口文件
（spec_from_file_location）加载 plugin.py，包内回导入插件类会在相对导入阶段
触发循环导入并导致插件注册失败。
"""
