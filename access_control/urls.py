from django.urls import path

from .views import AuditListView, EffectivePermissionView, GroupDetailView, GroupListCreateView, InvitationAcceptView, InvitationActionView, InvitationListCreateView, MemberDetailView, MemberListView, PermissionCatalogView, RoleDetailView, RoleDuplicateView, RoleListCreateView

urlpatterns = [
    path("invitations/accept/", InvitationAcceptView.as_view(), name="access-invitation-accept"),
    path("store/<int:store_id>/catalog/", PermissionCatalogView.as_view(), name="access-catalog"),
    path("store/<int:store_id>/me/", EffectivePermissionView.as_view(), name="access-effective"),
    path("store/<int:store_id>/roles/", RoleListCreateView.as_view(), name="access-roles"),
    path("store/<int:store_id>/roles/<int:role_id>/", RoleDetailView.as_view(), name="access-role-detail"),
    path("store/<int:store_id>/roles/<int:role_id>/duplicate/", RoleDuplicateView.as_view(), name="access-role-duplicate"),
    path("store/<int:store_id>/groups/", GroupListCreateView.as_view(), name="access-groups"),
    path("store/<int:store_id>/groups/<int:group_id>/", GroupDetailView.as_view(), name="access-group-detail"),
    path("store/<int:store_id>/members/", MemberListView.as_view(), name="access-members"),
    path("store/<int:store_id>/members/<int:member_id>/", MemberDetailView.as_view(), name="access-member-detail"),
    path("store/<int:store_id>/invitations/", InvitationListCreateView.as_view(), name="access-invitations"),
    path("store/<int:store_id>/invitations/<uuid:invitation_id>/<str:action>/", InvitationActionView.as_view(), name="access-invitation-action"),
    path("store/<int:store_id>/audit/", AuditListView.as_view(), name="access-audit"),
]
