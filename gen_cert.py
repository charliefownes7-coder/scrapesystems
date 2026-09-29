"""
Generates (once) a locally-trusted TLS certificate so the agent can
serve https://127.0.0.1:8765 instead of http://.

Why this exists: the Lovable dashboard is served over HTTPS. Browsers
block an HTTPS page from calling a plain http:// endpoint outright
("mixed content") — there's no user-facing way around that. Serving
the agent over HTTPS instead fixes it, but a plain self-signed cert
still throws a scary "not private" browser warning.

The fix: create our own local Certificate Authority once, ask the OS
(and, on Linux, the browser's own trust store) to trust it, then issue
the 127.0.0.1 cert from that trusted CA. After that one-time step, the
browser trusts the agent's cert automatically, with no warning page —
the same approach tools like mkcert use.
"""

import datetime
import ipaddress
import os
import platform
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

APP_DIR = os.path.expanduser("~/.scrapesystems")
os.makedirs(APP_DIR, exist_ok=True)

CA_KEY_PATH = os.path.join(APP_DIR, "ca_key.pem")
CA_CERT_PATH = os.path.join(APP_DIR, "ca_cert.pem")
LEAF_KEY_PATH = os.path.join(APP_DIR, "key.pem")
LEAF_CERT_PATH = os.path.join(APP_DIR, "cert.pem")
TRUST_MARKER_PATH = os.path.join(APP_DIR, "trust_attempted")


def _make_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ScrapeSystems Local CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    with open(CA_KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(CA_CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def _make_leaf(ca_key, ca_cert):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    with open(LEAF_KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(LEAF_CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _trust_ca_mac():
    # Writing to the SYSTEM trust store (so Safari/Chrome both honor
    # it without a browser restart) requires admin rights. Going
    # through osascript's "with administrator privileges" is what
    # actually surfaces the native macOS admin password/Touch ID
    # dialog — running the bare `security` command directly (as the
    # first version of this script did) can silently no-op on newer
    # macOS instead of prompting, which is why the browser never
    # ended up trusting the cert.
    #
    # Best-effort: if the user cancels the prompt, the agent still
    # runs — the browser will just show its self-signed-cert warning
    # once, and the person can click through it manually instead.
    quoted_path = CA_CERT_PATH.replace('"', '\\"')
    shell_cmd = (
        f'security add-trusted-cert -d -r trustRoot '
        f'-k /Library/Keychains/System.keychain "{quoted_path}"'
    )
    apple_script = f'do shell script "{shell_cmd}" with administrator privileges'
    try:
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            print(f"  Warning: couldn't add local certificate to the system trust store "
                  f"({result.stderr.strip()})")
    except Exception as e:
        print(f"  Warning: couldn't prompt for certificate trust ({e})")


def _trust_ca_windows():
    # -user (rather than -addstore Root without it) writes to the
    # CURRENT USER's Root store, not the machine-wide one — that's
    # deliberate: it's what lets this succeed without an elevated/UAC
    # prompt, since Chrome/Edge/Firefox-via-Windows-store all honor
    # the per-user Root store. The previous version of this function
    # never checked whether the command actually succeeded, so a
    # failure (missing certutil, corrupt cert file, etc.) passed
    # silently and the user was left with a half-trusted cert and no
    # explanation — same failure mode as the Linux one-time warning
    # path, just without the message telling them what to do instead.
    try:
        result = subprocess.run(
            ["certutil", "-addstore", "-user", "Root", CA_CERT_PATH],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode != 0:
            print(f"  Warning: couldn't add local certificate to the Windows trust store "
                  f"({result.stderr.strip() or result.stdout.strip()}). The browser will show "
                  f"a one-time certificate warning instead — click through it to continue.")
    except subprocess.TimeoutExpired:
        print("  Warning: Windows did not finish adding the local certificate in time. "
              "The browser will show a one-time certificate warning instead - click through it to continue.")
    except FileNotFoundError:
        print("  Warning: 'certutil' isn't available on this system, so the local certificate "
              "couldn't be trusted automatically. The browser will show a one-time certificate "
              "warning instead — click through it to continue.")
    except Exception as e:
        print(f"  Warning: couldn't add local certificate to Windows trust store automatically "
              f"({e}). The browser will show a one-time certificate warning instead — click "
              f"through it to continue.")


def _trust_ca_linux():
    # Linux has no single OS-wide trust prompt like macOS/Windows.
    # Chrome/Chromium on Linux reads its trusted certs from the NSS
    # database at ~/.pki/nssdb rather than the system CA store, so
    # that's the one that actually matters for the dashboard. This
    # requires the `certutil` binary from libnss3-tools (Debian/
    # Ubuntu/Crostini) or nss-tools (Fedora) to already be installed —
    # if it isn't, this is a no-op and the user falls back to
    # clicking through the browser's self-signed-cert warning once,
    # same as any other failed trust attempt on Mac/Windows.
    nssdb_dir = os.path.expanduser("~/.pki/nssdb")
    try:
        os.makedirs(nssdb_dir, exist_ok=True)
        # sql:~/.pki/nssdb is Chrome's default profile-independent NSS DB.
        subprocess.run(
            [
                "certutil", "-d", f"sql:{nssdb_dir}",
                "-A", "-t", "C,,",
                "-n", "ScrapeSystems Local CA",
                "-i", CA_CERT_PATH,
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        print("  Note: 'certutil' isn't installed (try: sudo apt install libnss3-tools), "
              "so the browser will show a one-time certificate warning instead of trusting "
              "it automatically.")
    except Exception as e:
        print(f"  Warning: couldn't add local certificate to the browser's trust store ({e})")

    # Best-effort: also try the system-wide CA store, in case something
    # other than Chrome (e.g. a different browser, or curl) needs it.
    # This step commonly requires sudo and may silently fail without it.
    try:
        system_ca_path = "/usr/local/share/ca-certificates/scrapesystems-local-ca.crt"
        subprocess.run(
            ["sudo", "-n", "cp", CA_CERT_PATH, system_ca_path],
            capture_output=True, text=True, check=False,
        )
        subprocess.run(
            ["sudo", "-n", "update-ca-certificates"],
            capture_output=True, text=True, check=False,
        )
    except Exception:
        # sudo -n fails immediately (no password prompt) if passwordless
        # sudo isn't set up — that's fine, this step is a bonus, not a
        # requirement. The NSS step above is what actually matters for
        # the dashboard itself.
        pass


def ensure_cert():
    """
    Returns (key_path, cert_path) for uvicorn's ssl_keyfile/ssl_certfile.
    Generates the CA + leaf cert on the very first call ever made on
    this machine. The OS-trust step is tracked separately from cert
    generation via TRUST_MARKER_PATH: this covers anyone who already
    generated certs under an older version of this script (before the
    trust step was fixed to use an admin-elevated prompt) by retrying
    the trust step once even though the certs themselves already exist.
    """
    have_certs = os.path.exists(CA_CERT_PATH) and os.path.exists(LEAF_CERT_PATH)
    if not have_certs:
        ca_key, ca_cert = _make_ca()
        _make_leaf(ca_key, ca_cert)

    if not os.path.exists(TRUST_MARKER_PATH):
        system = platform.system()
        if system == "Darwin":
            _trust_ca_mac()
        elif system == "Windows":
            _trust_ca_windows()
        elif system == "Linux":
            _trust_ca_linux()
        # Written regardless of success — if the user cancels the admin
        # prompt (or, on Linux, certutil isn't installed), we don't
        # want to nag them with it on every single launch. They can
        # always click through the browser's own self-signed-cert
        # warning instead.
        with open(TRUST_MARKER_PATH, "w") as f:
            f.write("attempted")

    return LEAF_KEY_PATH, LEAF_CERT_PATH
