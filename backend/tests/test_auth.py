from pathlib import Path

from fastapi import HTTPException, Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_configured_admin_user, get_current_user, resolve_session_token
from app.api.routes import (
    change_password,
    complete_admin_setup,
    delete_profile,
    login,
    logout,
    public_profile_summary,
    recover_admin_account,
    register,
    reset_password_by_pin,
    set_account_pin,
    switch_session,
    update_profile_permissions,
)
from app.db.init_db import seed_defaults
from app.models.base import Base
from app.models.entities import SessionToken, UserProfile
from app.schemas.common import (
    AdminRecoveryIn,
    AdminSetupIn,
    AdminUserPermissionIn,
    LoginIn,
    RegisterIn,
    SwitchSessionIn,
    UserPasswordChangeIn,
    UserPasswordResetByPinIn,
    UserPinSetIn,
)
from app.services import auth as auth_service
from app.services.auth import generate_recovery_phrase, verify_password
from app.services.auth import hash_session_token, is_hashed_session_token
from app.services.auth_rate_limit import clear_all_failures


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    clear_all_failures()
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


class DummyRequest:
    def __init__(self, host: str):
        self.client = type("Client", (), {"host": host})()
        self.headers: dict[str, str] = {}


def test_admin_bootstrap_setup_clears_pending_phrase(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))

        assert admin is not None
        assert admin.requires_admin_setup is True
        assert admin.recovery_phrase_pending

        result = complete_admin_setup(
            AdminSetupIn(password="admin123"),
            db=db,
            current_user=admin,
        )

        db.refresh(admin)

        assert result.is_admin is True
        assert result.requires_admin_setup is False
        assert admin.requires_admin_setup is False
        assert admin.recovery_phrase_pending is None
        assert verify_password("admin123", admin.password_hash) is True


def test_admin_bootstrap_setup_revokes_other_admin_sessions(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))

        assert admin is not None
        db.add_all(
            [
                SessionToken(token=hash_session_token("keep-session"), user_id=admin.id),
                SessionToken(token=hash_session_token("stale-session"), user_id=admin.id),
            ]
        )
        db.commit()

        complete_admin_setup(
            AdminSetupIn(password="admin123"),
            session_token="keep-session",
            db=db,
            current_user=admin,
        )

        remaining_tokens = db.scalars(select(SessionToken.token).where(SessionToken.user_id == admin.id)).all()
        assert remaining_tokens == [hash_session_token("keep-session")]


def test_seed_defaults_does_not_rotate_pending_admin_password(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))

        assert admin is not None
        original_password_hash = admin.password_hash
        original_phrase = admin.recovery_phrase_pending

        seed_defaults(db, [str(tmp_path / "library")])
        db.refresh(admin)

        assert admin.password_hash == original_password_hash
        assert admin.recovery_phrase_pending == original_phrase


def test_admin_recovery_phrase_can_reset_password(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))

        assert admin is not None
        assert admin.recovery_phrase_pending is not None

        response = Response()
        session = recover_admin_account(
            AdminRecoveryIn(
                recovery_phrase=admin.recovery_phrase_pending,
                password="recovered-admin-pass",
            ),
            response=response,
            request=DummyRequest("127.0.0.1"),
            db=db,
        )

        db.refresh(admin)

        assert session.user.is_admin is True
        assert session.user.requires_admin_setup is False
        assert verify_password("recovered-admin-pass", admin.password_hash) is True


def test_regular_user_cannot_access_configured_admin_dependency(tmp_path: Path):
    with make_session(tmp_path) as db:
        user = UserProfile(name="friend", display_name="Friend", accent_color="#fff")
        db.add(user)
        db.commit()

        try:
            get_configured_admin_user(current_user=user)
            assert False, "Expected admin dependency to reject regular user"
        except HTTPException as error:
            assert error.status_code == 403


def test_recovery_phrase_generator_creates_random_word_like_tokens():
    phrases = {generate_recovery_phrase() for _ in range(10)}

    assert len(phrases) > 1
    for phrase in phrases:
        words = phrase.split()
        assert len(words) == 6
        assert len(set(words)) == 6
        assert all(word.isalpha() for word in words)
        assert all(word == word.lower() for word in words)


def test_recovery_phrase_generator_falls_back_when_wordlist_is_missing(monkeypatch):
    auth_service._recovery_wordlist.cache_clear()
    monkeypatch.setattr(auth_service, "RECOVERY_WORDLIST_PATH", Path("missing-wordlist.txt"))

    phrase = auth_service.generate_recovery_phrase()

    auth_service._recovery_wordlist.cache_clear()

    words = phrase.split()
    assert len(words) == 6
    assert len(set(words)) == 6
    assert all(word in auth_service.FALLBACK_RECOVERY_WORDS for word in words)


def test_admin_can_promote_other_user(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")], include_demo_users=True)
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        guest = db.scalar(select(UserProfile).where(UserProfile.name == "guest"))

        assert admin is not None
        assert guest is not None

        updated = update_profile_permissions(
            guest.id,
            AdminUserPermissionIn(is_admin=True),
            db=db,
            current_user=admin,
        )

        assert updated.is_admin is True


def test_cannot_remove_last_admin(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))

        assert admin is not None

        try:
            update_profile_permissions(
                admin.id,
                AdminUserPermissionIn(is_admin=False),
                db=db,
                current_user=admin,
            )
            assert False, "Expected last-admin protection"
        except HTTPException as error:
            assert error.status_code == 400


def test_user_can_set_pin_once_and_reset_password_with_it(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")], include_demo_users=True)
        guest = db.scalar(select(UserProfile).where(UserProfile.name == "guest"))
        assert guest is not None

        set_account_pin(UserPinSetIn(pin="123456"), db=db, current_user=guest)
        db.refresh(guest)
        assert guest.pin_hash is not None

        response = Response()
        session = reset_password_by_pin(
            UserPasswordResetByPinIn(username="guest", pin="123456", password="guest-reset-pass"),
            response=response,
            request=DummyRequest("127.0.0.1"),
            db=db,
        )
        db.refresh(guest)
        assert session.user.name == "guest"
        assert verify_password("guest-reset-pass", guest.password_hash) is True

        try:
            set_account_pin(UserPinSetIn(pin="654321"), db=db, current_user=guest)
            assert False, "Expected immutable PIN protection"
        except HTTPException as error:
            assert error.status_code == 400


def test_public_profile_summary_reports_pin_status(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        guest = db.scalar(select(UserProfile).where(UserProfile.name == "guest"))
        assert guest is not None

        set_account_pin(UserPinSetIn(pin="123456"), db=db, current_user=guest)
        result = public_profile_summary("guest", db=db, current_user=guest)

        assert result["profile"].has_pin is True


def test_user_can_change_password_with_current_password(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")], include_demo_users=True)
        test_user = db.scalar(select(UserProfile).where(UserProfile.name == "test"))
        assert test_user is not None

        result = change_password(
            UserPasswordChangeIn(current_password="test", password="new-test-pass"),
            session_token=None,
            db=db,
            current_user=test_user,
        )
        db.refresh(test_user)
        assert result.name == "test"
        assert verify_password("new-test-pass", test_user.password_hash) is True


def test_create_session_stores_only_hashed_token(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])

        response = Response()
        session = login(LoginIn(username="guest", password="guest"), response=response, request=DummyRequest("127.0.0.1"), db=db)
        stored_tokens = db.scalars(select(SessionToken.token)).all()

        assert len(stored_tokens) == 1
        assert stored_tokens[0] != session.session_token
        assert is_hashed_session_token(stored_tokens[0]) is True
        assert stored_tokens[0] == hash_session_token(session.session_token)


def test_legacy_plaintext_session_token_is_migrated_on_use(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        guest = db.scalar(select(UserProfile).where(UserProfile.name == "guest"))
        assert guest is not None
        db.add(SessionToken(token="legacy-token", user_id=guest.id))
        db.commit()

        current_user = get_current_user(db=db, session_token="legacy-token")
        stored_token = db.scalar(select(SessionToken.token).where(SessionToken.user_id == guest.id))

        assert current_user.id == guest.id
        assert stored_token == hash_session_token("legacy-token")


def test_switch_and_logout_work_with_hashed_session_tokens(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        response = Response()
        session = login(LoginIn(username="guest", password="guest"), response=response, request=DummyRequest("127.0.0.1"), db=db)

        switched = switch_session(
            SwitchSessionIn(session_token=session.session_token),
            response=Response(),
            db=db,
        )
        assert switched.user.name == "guest"

        logout(response=Response(), session_token=session.session_token, db=db)
        assert resolve_session_token(db, session.session_token) is None


def test_admin_can_delete_user_profile(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")], include_demo_users=True)
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        test_user = db.scalar(select(UserProfile).where(UserProfile.name == "test"))
        assert admin is not None
        assert test_user is not None

        result = delete_profile(test_user.id, db=db, current_user=admin)
        assert result["ok"] is True
        assert db.scalar(select(UserProfile).where(UserProfile.name == "test")) is None


def test_builtin_guest_cannot_be_deleted(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        guest = db.scalar(select(UserProfile).where(UserProfile.name == "guest"))
        assert admin is not None
        assert guest is not None

        try:
            delete_profile(guest.id, db=db, current_user=admin)
            assert False, "Expected built-in guest protection"
        except HTTPException as error:
            assert error.status_code == 400


def test_seed_defaults_only_bootstraps_admin_by_default(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])

        profiles = db.scalars(select(UserProfile).order_by(UserProfile.id)).all()

        assert [profile.name for profile in profiles] == ["admin", "guest"]


def test_register_normalizes_username_and_blocks_case_variant_duplicates(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)

        response = Response()
        session = register(
            RegisterIn(username="  Friend.User  ", password="friendpass", pin="123456", display_name="Friend"),
            response=response,
            db=db,
        )

        assert session.user.name == "friend.user"

        try:
            register(
                RegisterIn(username="FRIEND.USER", password="otherpass", pin="654321"),
                response=Response(),
                db=db,
            )
            assert False, "Expected case-insensitive duplicate username protection"
        except HTTPException as error:
            assert error.status_code == 409


def test_register_rejects_invalid_username_characters(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)

        try:
            register(
                RegisterIn(username="bad/name", password="friendpass", pin="123456"),
                response=Response(),
                db=db,
            )
            assert False, "Expected invalid username validation"
        except HTTPException as error:
            assert error.status_code == 400


def test_login_accepts_trimmed_case_insensitive_username(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)
        register(
            RegisterIn(username="friend", password="friendpass", pin="123456"),
            response=Response(),
            db=db,
        )

        session = login(
            LoginIn(username="  FRIEND  ", password="friendpass"),
            response=Response(),
            request=DummyRequest("127.0.0.1"),
            db=db,
        )

        assert session.user.name == "friend"


def test_reset_password_by_pin_accepts_trimmed_case_insensitive_username(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)
        register(
            RegisterIn(username="friend", password="friendpass", pin="123456"),
            response=Response(),
            db=db,
        )

        session = reset_password_by_pin(
            UserPasswordResetByPinIn(username="  FRIEND ", pin="123456", password="updatedpass"),
            response=Response(),
            request=DummyRequest("127.0.0.1"),
            db=db,
        )
        user = db.scalar(select(UserProfile).where(UserProfile.name == "friend"))

        assert session.user.name == "friend"
        assert user is not None
        assert verify_password("updatedpass", user.password_hash) is True


def test_change_password_revokes_other_sessions(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)
        session = register(
            RegisterIn(username="friend", password="friendpass", pin="123456"),
            response=Response(),
            db=db,
        )
        user = db.scalar(select(UserProfile).where(UserProfile.name == "friend"))
        assert user is not None

        db.add(SessionToken(token="old-session", user_id=user.id))
        db.commit()

        change_password(
            UserPasswordChangeIn(current_password="friendpass", password="updatedpass"),
            session_token=session.session_token,
            db=db,
            current_user=user,
        )

        remaining_tokens = db.scalars(select(SessionToken.token).where(SessionToken.user_id == user.id)).all()
        assert remaining_tokens == [hash_session_token(session.session_token)]


def test_logout_revokes_current_session(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)
        session = register(
            RegisterIn(username="friend", password="friendpass", pin="123456"),
            response=Response(),
            db=db,
        )

        result = logout(Response(), session_token=session.session_token, db=db)
        remaining = resolve_session_token(db, session.session_token)

        assert result["ok"] is True
        assert remaining is None


def test_login_rate_limit_blocks_repeated_failures(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        request = DummyRequest("10.0.0.8")

        for _ in range(8):
            try:
                login(LoginIn(username="guest", password="wrong"), response=Response(), request=request, db=db)
            except HTTPException as error:
                assert error.status_code == 401

        try:
            login(LoginIn(username="guest", password="wrong"), response=Response(), request=request, db=db)
            assert False, "Expected login rate limit"
        except HTTPException as error:
            assert error.status_code == 429


def test_password_reset_rate_limit_blocks_repeated_failures(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        admin = db.scalar(select(UserProfile).where(UserProfile.name == "admin"))
        assert admin is not None
        complete_admin_setup(AdminSetupIn(password="admin123"), db=db, current_user=admin)
        register(
            RegisterIn(username="friend", password="friendpass", pin="123456"),
            response=Response(),
            db=db,
        )
        request = DummyRequest("10.0.0.9")

        for _ in range(5):
            try:
                reset_password_by_pin(
                    UserPasswordResetByPinIn(username="friend", pin="000000", password="updatedpass"),
                    response=Response(),
                    request=request,
                    db=db,
                )
            except HTTPException as error:
                assert error.status_code == 401

        try:
            reset_password_by_pin(
                UserPasswordResetByPinIn(username="friend", pin="000000", password="updatedpass"),
                response=Response(),
                request=request,
                db=db,
            )
            assert False, "Expected password reset rate limit"
        except HTTPException as error:
            assert error.status_code == 429


def test_admin_recovery_rate_limit_blocks_repeated_failures(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])
        request = DummyRequest("10.0.0.10")

        for _ in range(5):
            try:
                recover_admin_account(
                    AdminRecoveryIn(recovery_phrase="wrong phrase", password="recovered-admin-pass"),
                    response=Response(),
                    request=request,
                    db=db,
                )
            except HTTPException as error:
                assert error.status_code == 401

        try:
            recover_admin_account(
                AdminRecoveryIn(recovery_phrase="wrong phrase", password="recovered-admin-pass"),
                response=Response(),
                request=request,
                db=db,
            )
            assert False, "Expected admin recovery rate limit"
        except HTTPException as error:
            assert error.status_code == 429
