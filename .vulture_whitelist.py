# ruff: noqa: F821
# Framework callbacks vulture cannot see being called: http.server handlers, MCP tools
# registered by decorator, and the urllib redirect hook used in tests.
main  # unused function (src\cartclaw\__init__.py:4)
_.log_message  # unused method (src\cartclaw\approval.py:124)
fmt  # unused variable (src\cartclaw\approval.py:126)
_.do_GET  # unused method (src\cartclaw\approval.py:157)
_.do_POST  # unused method (src\cartclaw\approval.py:177)
checkout_request  # unused function (src\cartclaw\server.py:103)
checkout_status  # unused function (src\cartclaw\server.py:122)
_.redirect_request  # unused method (tests\test_approval.py:18)
kwargs  # unused variable (tests\test_approval.py:18)
