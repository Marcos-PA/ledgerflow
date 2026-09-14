from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.models import UserModel
from ..infrastructure.security import hash_password, verify_password

ROLES = ("requester", "compliance_officer", "authorizer", "admin")


class AuthServiceError(Exception):
    pass


class EmailAlreadyRegisteredError(AuthServiceError):
    pass


class InvalidCredentialsError(AuthServiceError):
    pass


class AuthService:
    def __init__(self, session: Session):
        self.session = session

    def register(self, *, email: str, password: str, role: str) -> UserModel:
        user = UserModel(email=email, password_hash=hash_password(password), role=role)
        self.session.add(user)
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise EmailAlreadyRegisteredError("email already registered") from error
        self.session.refresh(user)
        return user

    def authenticate(self, *, email: str, password: str) -> UserModel:
        user = self.session.scalar(select(UserModel).where(UserModel.email == email))
        if user is None or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError("invalid email or password")
        return user
