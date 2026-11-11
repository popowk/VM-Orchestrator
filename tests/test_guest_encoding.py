"""Regression test the Guest Agent Windows console decoder without importing
Windows-only runtime modules into the Linux release build."""
import ast
from pathlib import Path
import os
import unittest


class CommandEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path('guest_agent/agent_v2.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        method = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == 'decode_command_output')
        namespace = {'os': os}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<decoder>', 'exec'), namespace)
        cls.decode = staticmethod(namespace['decode_command_output'])

    def test_utf8_chinese_text(self):
        message = '文件已经被另一个程序使用'
        self.assertEqual(self.decode(message.encode('utf-8')), message)

    def test_legacy_gbk_chinese_text(self):
        message = '另一个程序正在使用此文件，进程无法访问'
        self.assertEqual(self.decode(message.encode('gbk')), message)

    def test_utf16_bom_and_ascii(self):
        self.assertEqual(self.decode('操作失败'.encode('utf-16')), '操作失败')
        self.assertEqual(self.decode(b'Access denied'), 'Access denied')


if __name__ == '__main__':
    unittest.main()
