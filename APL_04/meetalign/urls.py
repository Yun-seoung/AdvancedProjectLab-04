from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("signup/", views.signup, name="signup"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("teams/join/", views.team_join, name="team_join"),
    path("teams/<int:team_id>/meetings/", views.meeting_list, name="meeting_list"),
    path("teams/<int:team_id>/meetings/new/", views.meeting_create, name="meeting_create"),
    path("meetings/<int:meeting_id>/", views.meeting_detail, name="meeting_detail"),
    path("meetings/<int:meeting_id>/result/", views.meeting_result, name="meeting_result"),
    path("meetings/<int:meeting_id>/minutes.md", views.minutes_download, name="minutes_download"),
    path("meetings/<int:meeting_id>/quiz/review/", views.quiz_review, name="quiz_review"),
    path("meetings/<int:meeting_id>/quiz/stats/", views.quiz_stats, name="quiz_stats"),
    path("meetings/<int:meeting_id>/chat/", views.chat, name="chat"),
    path("meetings/<int:meeting_id>/inbox/", views.inbox, name="inbox"),
    path("meetings/<int:meeting_id>/summary/", views.summary, name="summary"),
    path("meetings/<int:meeting_id>/answers/", views.answers, name="answers"),
    path("meetings/<int:meeting_id>/understanding/", views.understanding, name="understanding"),
]
