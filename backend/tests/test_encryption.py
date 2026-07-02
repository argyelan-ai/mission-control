from app.services.encryption import encrypt


def test_encrypt_docstring_documents_plaintext_and_base64_ciphertext():
    docstring = encrypt.__doc__

    assert docstring is not None
    assert "Args:" in docstring
    assert "plaintext:" in docstring
    assert "Returns:" in docstring
    assert "base64" in docstring
    assert "Fernet" in docstring
