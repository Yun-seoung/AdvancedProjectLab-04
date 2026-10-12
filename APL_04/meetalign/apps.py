from django.apps import AppConfig


class MeetalignConfig(AppConfig):
    name = 'meetalign'
    # 기존 마이그레이션(0001_initial)이 BigAutoField로 생성돼 있어서 명시적으로 고정해둔다.
    # (안 고정하면 Django 버전마다 기본 PK 타입 판단이 달라져서
    #  makemigrations가 불필요한 "Alter field id" 변경을 계속 감지한다.)
    default_auto_field = 'django.db.models.BigAutoField'
